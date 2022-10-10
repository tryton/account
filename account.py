# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime
import operator
from collections import defaultdict
from decimal import Decimal
from functools import wraps
from itertools import zip_longest

from dateutil.relativedelta import relativedelta
from sql import Column, Literal, Null, Window
from sql.aggregate import Count, Max, Min, Sum
from sql.conditionals import Case, Coalesce

from trytond import backend
from trytond.i18n import gettext
from trytond.model import (
    Check, Index, ModelSQL, ModelView, Unique, fields, sequence_ordered, tree)
from trytond.model.exceptions import AccessError
from trytond.modules.currency.fields import Monetary
from trytond.pool import Pool
from trytond.pyson import Bool, Eval, If, PYSONEncoder
from trytond.report import Report
from trytond.tools import (
    grouped_slice, is_full_text, lstrip_wildcard, reduce_ids)
from trytond.transaction import Transaction
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)

from .common import ActivePeriodMixin, ContextCompanyMixin, PeriodMixin
from .exceptions import (
    AccountValidationError, ChartWarning, SecondCurrencyError)


def inactive_records(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with Transaction().set_context(active_test=False):
            return func(*args, **kwargs)
    return wrapper


def TypeMixin(template=False):

    class Mixin:
        __slots__ = ()
        name = fields.Char('Name', required=True)

        statement = fields.Selection([
                (None, ""),
                ('balance', "Balance"),
                ('income', "Income"),
                ('off-balance', "Off-Balance"),
                ], "Statement",
            states={
                'required': Bool(Eval('parent')),
                })
        assets = fields.Boolean(
            "Assets",
            states={
                'invisible': Eval('statement') != 'balance',
                })

        receivable = fields.Boolean(
            "Receivable",
            domain=[
                If((Eval('statement') != 'balance')
                    | ~Eval('assets', True),
                    ('receivable', '=', False), ()),
                ],
            states={
                'invisible': ((Eval('statement') != 'balance')
                    | ~Eval('assets', True)),
                })
        stock = fields.Boolean(
            "Stock",
            domain=[
                If(Eval('statement') == 'off-balance',
                    ('stock', '=', False), ()),
                ],
            states={
                'invisible': Eval('statement') == 'off-balance',
                })

        payable = fields.Boolean(
            "Payable",
            domain=[
                If((Eval('statement') != 'balance')
                    | Eval('assets', False),
                    ('payable', '=', False), ()),
                ],
            states={
                'invisible': ((Eval('statement') != 'balance')
                    | Eval('assets', False)),
                })

        debt = fields.Boolean(
            "Debt",
            domain=[
                If((Eval('statement') != 'balance')
                    | Eval('assets', False),
                    ('debt', '=', False), ()),
                ],
            states={
                'invisible': ((Eval('statement') != 'balance')
                    | Eval('assets', False)),
                },
            help="Check to allow booking debt via supplier invoice.")

        revenue = fields.Boolean(
            "Revenue",
            domain=[
                If(Eval('statement') != 'income',
                    ('revenue', '=', False), ()),
                ],
            states={
                'invisible': Eval('statement') != 'income',
                })

        expense = fields.Boolean(
            "Expense",
            domain=[
                If(Eval('statement') != 'income',
                    ('expense', '=', False), ()),
                ],
            states={
                'invisible': Eval('statement') != 'income',
                })
    if not template:
        for fname in dir(Mixin):
            field = getattr(Mixin, fname)
            if not isinstance(field, fields.Field):
                continue
            field.states['readonly'] = (
                Bool(Eval('template', -1)) & ~Eval('template_override', False))
    return Mixin


class TypeTemplate(
        TypeMixin(template=True), sequence_ordered(), tree(separator='\\'),
        ModelSQL, ModelView):
    'Account Type Template'
    __name__ = 'account.account.type.template'
    parent = fields.Many2One(
        'account.account.type.template', "Parent", ondelete='RESTRICT',
        domain=['OR',
            If(Eval('statement') == 'off-balance',
                ('statement', '=', 'off-balance'),
                If(Eval('statement') == 'balance',
                    ('statement', '=', 'balance'),
                    ('statement', '!=', 'off-balance')),
                ),
            ('statement', '=', None),
            ])
    childs = fields.One2Many(
        'account.account.type.template', 'parent', "Children")

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)
        table_h = cls.__table_handler__(module_name)

        # Migration from 5.0: remove display_balance
        table_h.drop_column('display_balance')

    def _get_type_value(self, type=None):
        '''
        Set the values for account creation.
        '''
        res = {}
        if not type or type.name != self.name:
            res['name'] = self.name
        if not type or type.sequence != self.sequence:
            res['sequence'] = self.sequence
        if not type or type.statement != self.statement:
            res['statement'] = self.statement
        if not type or type.assets != self.assets:
            res['assets'] = self.assets
        for boolean in [
                'receivable', 'stock', 'payable', 'revenue', 'expense',
                'debt']:
            if not type or getattr(type, boolean) != getattr(self, boolean):
                res[boolean] = getattr(self, boolean)
        if not type or type.template != self:
            res['template'] = self.id
        return res

    def create_type(self, company_id, template2type=None):
        '''
        Create recursively types based on template.
        template2type is a dictionary with template id as key and type id as
        value, used to convert template id into type. The dictionary is filled
        with new types.
        '''
        pool = Pool()
        Type = pool.get('account.account.type')
        assert self.parent is None

        if template2type is None:
            template2type = {}

        def create(templates):
            values = []
            created = []
            for template in templates:
                if template.id not in template2type:
                    vals = template._get_type_value()
                    vals['company'] = company_id
                    if template.parent:
                        vals['parent'] = template2type[template.parent.id]
                    else:
                        vals['parent'] = None
                    values.append(vals)
                    created.append(template)

            types = Type.create(values)
            for template, type_ in zip(created, types):
                template2type[template.id] = type_.id

        childs = [self]
        while childs:
            create(childs)
            childs = sum((c.childs for c in childs), ())


class Type(
        TypeMixin(), sequence_ordered(), tree(separator='\\'),
        ModelSQL, ModelView):
    'Account Type'
    __name__ = 'account.account.type'

    parent = fields.Many2One('account.account.type', 'Parent',
        ondelete="RESTRICT",
        states={
            'readonly': (Bool(Eval('template', -1))
                & ~Eval('template_override', False)),
            },
        domain=[
            ('company', '=', Eval('company', -1)),
            ['OR',
                If(Eval('statement') == 'off-balance',
                    ('statement', '=', 'off-balance'),
                    If(Eval('statement') == 'balance',
                        ('statement', '=', 'balance'),
                        ('statement', '!=', 'off-balance')),
                    ),
                ('statement', '=', None),
                ],
            ])
    childs = fields.One2Many('account.account.type', 'parent', 'Children',
        domain=[
            ('company', '=', Eval('company', -1)),
            ])
    currency = fields.Function(fields.Many2One(
        'currency.currency', 'Currency'), 'get_currency')
    amount = fields.Function(Monetary(
            "Amount", currency='currency', digits='currency'),
        'get_amount')
    amount_cmp = fields.Function(Monetary(
            "Amount", currency='currency', digits='currency'),
        'get_amount_cmp')

    company = fields.Many2One('company.company', 'Company', required=True,
            ondelete="RESTRICT")
    template = fields.Many2One('account.account.type.template', 'Template')
    template_override = fields.Boolean('Override Template',
        help="Check to override template definition",
        states={
            'invisible': ~Bool(Eval('template', -1)),
            })

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)
        table_h = cls.__table_handler__(module_name)

        # Migration from 5.0: remove display_balance
        table_h.drop_column('display_balance')

    @classmethod
    def default_template_override(cls):
        return False

    @classmethod
    def default_company(cls):
        return Transaction().context.get('company')

    @fields.depends('parent', '_parent_parent.statement')
    def on_change_parent(self):
        if self.parent:
            self.statement = self.parent.statement

    def get_currency(self, name):
        return self.company.currency.id

    @classmethod
    def get_amount(cls, types, name):
        pool = Pool()
        Account = pool.get('account.account')
        GeneralLedger = pool.get('account.general_ledger.account')
        context = Transaction().context

        res = {}
        for type_ in types:
            res[type_.id] = Decimal('0.0')

        childs = cls.search([
                ('parent', 'child_of', [t.id for t in types]),
                ])
        type_sum = {}
        for type_ in childs:
            type_sum[type_.id] = Decimal('0.0')

        period_ids = from_date = to_date = None
        if context.get('start_period') or context.get('end_period'):
            start_period_ids = GeneralLedger.get_period_ids('start_%s' % name)
            end_period_ids = GeneralLedger.get_period_ids('end_%s' % name)
            period_ids = list(
                set(end_period_ids).difference(set(start_period_ids)))
        elif context.get('from_date') or context.get('to_date'):
            from_date, _ = GeneralLedger.get_dates('start_%s' % name)
            _, to_date = GeneralLedger.get_dates('end_%s' % name)

        with Transaction().set_context(
                periods=period_ids, from_date=from_date, to_date=to_date):
            accounts = Account.search([
                    ('type', 'in', [t.id for t in childs]),
                    ])
            debit_credit_accounts = Account.search([
                    ('type', '!=', None),
                    ['OR',
                        ('debit_type', 'in', [t.id for t in childs]),
                        ('credit_type', 'in', [t.id for t in childs]),
                        ],
                    ])
        for account in accounts:
            balance = account.credit - account.debit
            if ((not account.debit_type or balance > 0)
                    and (not account.credit_type or balance < 0)):
                type_sum[account.type.id] += balance
        for account in debit_credit_accounts:
            balance = account.credit - account.debit
            if account.debit_type and balance < 0:
                type_sum[account.debit_type.id] += balance
            elif account.credit_type and balance > 0:
                type_sum[account.credit_type.id] += balance

        for type_ in types:
            childs = cls.search([
                    ('parent', 'child_of', [type_.id]),
                    ])
            for child in childs:
                res[type_.id] += type_sum[child.id]
            res[type_.id] = type_.currency.round(res[type_.id])
            if type_.statement == 'balance' and type_.assets:
                res[type_.id] = - res[type_.id]
        return res

    @classmethod
    def get_amount_cmp(cls, types, name):
        transaction = Transaction()
        current = transaction.context
        if not current.get('comparison'):
            return dict.fromkeys([t.id for t in types], None)
        new = {}
        for key, value in current.items():
            if key.endswith('_cmp'):
                new[key[:-4]] = value
        with transaction.set_context(new):
            return cls.get_amount(types, name)

    @classmethod
    def view_attributes(cls):
        return super().view_attributes() + [
            ('/tree/field[@name="amount_cmp"]', 'tree_invisible',
                ~Eval('comparison', False)),
            ]

    @classmethod
    def copy(cls, types, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('template', None)
        return super().copy(types, default=default)

    @classmethod
    def delete(cls, types):
        types = cls.search([
                ('parent', 'child_of', [t.id for t in types]),
                ])
        super(Type, cls).delete(types)

    def update_type(self, template2type=None):
        '''
        Update recursively types based on template.
        template2type is a dictionary with template id as key and type id as
        value, used to convert template id into type. The dictionary is filled
        with new types
        '''
        if template2type is None:
            template2type = {}

        values = []
        childs = [self]
        while childs:
            for child in childs:
                if child.template:
                    if not child.template_override:
                        vals = child.template._get_type_value(type=child)
                        if vals:
                            values.append([child])
                            values.append(vals)
                    template2type[child.template.id] = child.id
            childs = sum((c.childs for c in childs), ())
        if values:
            self.write(*values)

        # Update parent
        to_save = []
        childs = [self]
        while childs:
            for child in childs:
                if child.template:
                    if not child.template_override:
                        if child.template.parent:
                            parent = template2type[
                                child.template.parent.id]
                        else:
                            parent = None
                        old_parent = (
                            child.parent.id if child.parent else None)
                        if parent != old_parent:
                            child.parent = parent
                            to_save.append(child)
            childs = sum((c.childs for c in childs), ())
        self.__class__.save(to_save)


class OpenType(Wizard):
    'Open Type'
    __name__ = 'account.account.open_type'
    start = StateTransition()
    account = StateAction('account.act_account_balance_sheet')
    ledger_account = StateAction('account.act_account_general_ledger')

    def transition_start(self):
        context_model = Transaction().context.get('context_model')
        if context_model == 'account.balance_sheet.comparison.context':
            return 'account'
        elif context_model == 'account.income_statement.context':
            return 'ledger_account'
        return 'end'

    def open_action(self, action):
        pool = Pool()
        action['name'] = '%s (%s)' % (action['name'], self.record.rec_name)
        trans_context = Transaction().context
        context = {
            'active_id': trans_context.get('active_id'),
            'active_ids': trans_context.get('active_ids', []),
            'active_model': trans_context.get('active_model'),
            }
        context_model = trans_context.get('context_model')
        if context_model:
            Model = pool.get(context_model)
            for fname in Model._fields.keys():
                if fname == 'id':
                    continue
                context[fname] = trans_context.get(fname)
        action['pyson_context'] = PYSONEncoder().encode(context)
        return action, {}

    do_account = open_action
    do_ledger_account = open_action


class AccountTypeStatement(Report):
    __name__ = 'account.account.type.statement'

    @classmethod
    def get_context(cls, records, header, data):
        pool = Pool()
        Company = pool.get('company.company')
        context = Transaction().context

        report_context = super().get_context(records, header, data)
        report_context['company'] = Company(context['company'])

        if data.get('model_context') is not None:
            Context = pool.get(data['model_context'])
            values = {}
            for field in Context._fields:
                if field in context:
                    values[field] = context[field]
            report_context['ctx'] = Context(**values)

        report_context['types'] = zip_longest(
            records, data.get('paths') or [], fillvalue=[])
        return report_context


def AccountMixin(template=False):

    class Mixin:
        __slots__ = ()
        _order_name = 'rec_name'
        name = fields.Char("Name", required=True)
        code = fields.Char("Code")

        closed = fields.Boolean(
            "Closed",
            states={
                'invisible': ~Eval('type'),
                },
            help="Check to prevent posting move on the account.")
        reconcile = fields.Boolean(
            "Reconcile",
            states={
                'invisible': ~Eval('type'),
                },
            help="Allow move lines of this account to be reconciled.")

        party_required = fields.Boolean('Party Required',
            domain=[
                If(~Eval('type') | ~Eval('deferral', False),
                    ('party_required', '=', False),
                    ()),
                ],
            states={
                'invisible': ~Eval('type') | ~Eval('deferral', False),
                })

        general_ledger_balance = fields.Boolean('General Ledger Balance',
            states={
                'invisible': ~Eval('type'),
                },
            help="Display only the balance in the general ledger report.")

        deferral = fields.Function(fields.Boolean(
                "Deferral",
                states={
                    'invisible': ~Eval('type'),
                    }),
            'on_change_with_deferral', searcher='search_deferral')

        @classmethod
        def __setup__(cls):
            super().__setup__()

            if not cls.childs.domain:
                cls.childs.domain = []
            for type_ in ['type', 'debit_type']:
                field = getattr(cls, type_)
                field.domain = [
                    If(Eval('parent')
                        & Eval('_parent_parent.%s' % type_)
                        & Eval('_parent_parent.parent'),
                        ('id', '=', Eval('_parent_parent.%s' % type_)),
                        ()),
                    ]

                cls.childs.domain.append(
                    If(Eval(type_) & Eval('parent'),
                        (type_, '=', Eval(type_)),
                        ()))

        @classmethod
        def default_closed(cls):
            return False

        @classmethod
        def default_reconcile(cls):
            return False

        @classmethod
        def default_party_required(cls):
            return False

        @classmethod
        def default_general_ledger_balance(cls):
            return False

        @fields.depends('type')
        def on_change_with_deferral(self, name=None):
            return (self.type
                and self.type.statement in {'balance', 'off-balance'})

        @classmethod
        def search_deferral(cls, name, clause):
            _, operator, value = clause
            if operator in {'in', 'not in'}:
                if operator == 'in':
                    operator = '='
                else:
                    operator = '!='
                if True in value and False not in value:
                    value = '=', True
                elif False in value and True not in value:
                    value = '=', False
                else:
                    return [('id', operator, None)]
            if ((operator == '=' and value)
                    or (operator == '!=' and not value)):
                return [
                    ('type.statement', 'in', ['balance', 'off-balance']),
                    ]
            else:
                return ['OR',
                    ('type', '=', None),
                    ('type.statement', 'not in', ['balance', 'off-balance']),
                    ]

        def get_rec_name(self, name):
            if self.code:
                return self.code + ' - ' + self.name
            else:
                return self.name

        @classmethod
        def search_rec_name(cls, name, clause):
            _, operator, operand, *extra = clause
            if operator.startswith('!') or operator.startswith('not '):
                bool_op = 'AND'
            else:
                bool_op = 'OR'
            code_value = operand
            if operator.endswith('like') and is_full_text(operand):
                code_value = lstrip_wildcard(operand)
            return [bool_op,
                ('code', operator, code_value, *extra),
                (cls._rec_name, operator, operand, *extra),
                ]

        @staticmethod
        def order_rec_name(tables):
            table, _ = tables[None]
            return [table.code, table.name]

    if not template:
        for fname in dir(Mixin):
            field = getattr(Mixin, fname)
            if (not isinstance(field, fields.Field)
                    or isinstance(field, fields.Function)):
                continue
            field.states['readonly'] = (
                Bool(Eval('template', -1)) & ~Eval('template_override', False))
    return Mixin


class AccountTemplate(
        AccountMixin(template=True), PeriodMixin, tree(), ModelSQL, ModelView):
    'Account Template'
    __name__ = 'account.account.template'
    type = fields.Many2One(
        'account.account.type.template', "Type", ondelete="RESTRICT")
    debit_type = fields.Many2One(
        'account.account.type.template', "Debit Type", ondelete="RESTRICT")
    credit_type = fields.Many2One(
        'account.account.type.template', "Credit Type", ondelete="RESTRICT")
    parent = fields.Many2One(
        'account.account.template', "Parent", ondelete="RESTRICT")
    childs = fields.One2Many('account.account.template', 'parent', 'Children')
    taxes = fields.Many2Many('account.account.template-account.tax.template',
            'account', 'tax', 'Default Taxes',
            domain=[('parent', '=', None)])
    replaced_by = fields.Many2One(
        'account.account.template', "Replaced By",
        states={
            'invisible': ~Eval('end_date'),
            })

    @classmethod
    def __setup__(cls):
        super(AccountTemplate, cls).__setup__()
        cls._order.insert(0, ('code', 'ASC'))
        cls._order.insert(1, ('name', 'ASC'))
        table = cls.__table__()
        cls._sql_constraints.append(
            ('only_one_debit_credit_types', Check(
                    table, (table.debit_type + table.credit_type) == Null),
                'account.msg_only_one_debit_credit_types'))

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)

        table_h = cls.__table_handler__(module_name)

        # Migration from 5.0: remove kind
        table_h.drop_column('kind')

    def _get_account_value(self, account=None):
        '''
        Set the values for account creation.
        '''
        res = {}
        if not account or account.name != self.name:
            res['name'] = self.name
        if not account or account.code != self.code:
            res['code'] = self.code
        if not account or account.start_date != self.start_date:
            res['start_date'] = self.start_date
        if not account or account.end_date != self.end_date:
            res['end_date'] = self.end_date
        if not account or account.closed != self.closed:
            res['closed'] = self.closed
        if not account or account.reconcile != self.reconcile:
            res['reconcile'] = self.reconcile
        if not account or account.party_required != self.party_required:
            res['party_required'] = self.party_required
        if (not account
                or account.general_ledger_balance
                != self.general_ledger_balance):
            res['general_ledger_balance'] = self.general_ledger_balance
        if not account or account.template != self:
            res['template'] = self.id
        return res

    def create_account(self, company_id, template2account=None,
            template2type=None):
        '''
        Create recursively accounts based on template.
        template2account is a dictionary with template id as key and account id
        as value, used to convert template id into account. The dictionary is
        filled with new accounts
        template2type is a dictionary with type template id as key and type id
        as value, used to convert type template id into type.
        '''
        pool = Pool()
        Account = pool.get('account.account')
        assert self.parent is None

        if template2account is None:
            template2account = {}

        if template2type is None:
            template2type = {}

        def create(templates):
            values = []
            created = []
            for template in templates:
                if template.id not in template2account:
                    vals = template._get_account_value()
                    vals['company'] = company_id
                    if template.parent:
                        vals['parent'] = template2account[template.parent.id]
                    else:
                        vals['parent'] = None
                    if template.type:
                        vals['type'] = template2type.get(template.type.id)
                    else:
                        vals['type'] = None
                    if template.debit_type:
                        vals['debit_type'] = template2type.get(
                            template.debit_type.id)
                    else:
                        vals['debit_type'] = None
                    if template.credit_type:
                        vals['credit_type'] = template2type.get(
                            template.credit_type.id)
                    else:
                        vals['credit_type'] = None
                    values.append(vals)
                    created.append(template)

            accounts = Account.create(values)
            for template, account in zip(created, accounts):
                template2account[template.id] = account.id

        childs = [self]
        while childs:
            create(childs)
            childs = sum((c.childs for c in childs), ())

    def update_account2(self, template2account, template2tax,
            template_done=None):
        '''
        Update recursively account taxes and replaced_by based on template.
        template2account is a dictionary with template id as key and account id
        as value, used to convert template id into account.
        template2tax is a dictionary with tax template id as key and tax id as
        value, used to convert tax template id into tax.
        template_done is a list of template id already updated. The list is
        filled.
        '''
        Account = Pool().get('account.account')

        if template2account is None:
            template2account = {}
        if template2tax is None:
            template2tax = {}
        if template_done is None:
            template_done = []

        def update(templates):
            to_write = []
            for template in templates:
                if template.id not in template_done:
                    template_done.append(template.id)
                    account = Account(template2account[template.id])
                    if account.template_override:
                        continue
                    values = {}
                    if template.taxes:
                        tax_ids = [template2tax[x.id] for x in template.taxes]
                        values['taxes'] = [('add', tax_ids)]
                    if template.replaced_by:
                        values['replaced_by'] = template2account[
                            template.replaced_by.id]
                    if values:
                        to_write.append([account])
                        to_write.append(values)
            if to_write:
                Account.write(*to_write)

        childs = [self]
        while childs:
            update(childs)
            childs = sum((c.childs for c in childs), ())


class AccountTemplateTaxTemplate(ModelSQL):
    'Account Template - Tax Template'
    __name__ = 'account.account.template-account.tax.template'
    _table = 'account_account_template_tax_rel'
    account = fields.Many2One(
        'account.account.template', "Account Template",
        ondelete='CASCADE', required=True)
    tax = fields.Many2One(
        'account.tax.template', "Tax Template",
        ondelete='RESTRICT', required=True)


class Account(
        AccountMixin(), ContextCompanyMixin, ActivePeriodMixin, tree(),
        ModelSQL, ModelView):
    'Account'
    __name__ = 'account.account'
    _states = {
        'readonly': (Bool(Eval('template', -1))
            & ~Eval('template_override', False)),
        }
    company = fields.Many2One('company.company', 'Company', required=True,
            ondelete="RESTRICT")
    currency = fields.Function(fields.Many2One('currency.currency',
        'Currency'), 'get_currency')
    second_currency = fields.Many2One('currency.currency',
        'Secondary Currency', help='Force all moves for this account \n'
        'to have this secondary currency.', ondelete="RESTRICT",
        domain=[
            ('id', '!=', Eval('currency', -1)),
            ],
        states={
            'readonly': _states['readonly'],
            'invisible': ~Eval('deferral', False),
            })
    type = fields.Many2One(
        'account.account.type', "Type", ondelete='RESTRICT',
        states={
            'readonly': _states['readonly'],
            },
        domain=[
            ('company', '=', Eval('company')),
            ])
    debit_type = fields.Many2One(
        'account.account.type', "Debit Type", ondelete='RESTRICT',
        states={
            'readonly': _states['readonly'],
            'invisible': (
                ~Eval('type') | Eval('credit_type')
                | (_states['readonly']) & ~Eval('debit_type')),
            },
        domain=[
            ('company', '=', Eval('company')),
            ],
        help="The type used if not empty and debit > credit.")
    credit_type = fields.Many2One(
        'account.account.type', "Credit Type", ondelete='RESTRICT',
        states={
            'readonly': _states['readonly'],
            'invisible': (
                ~Eval('type') | Eval('debit_type')
                | (_states['readonly']) & ~Eval('credit_type')),
            },
        domain=[
            ('company', '=', Eval('company')),
            ],
        help="The type used if not empty and debit < credit.")
    parent = fields.Many2One(
        'account.account', "Parent",
        left="left", right="right", ondelete="RESTRICT", states=_states,
        domain=[
            ('company', '=', Eval('company', -1)),
            ])
    left = fields.Integer("Left", required=True)
    right = fields.Integer("Right", required=True)
    childs = fields.One2Many(
        'account.account', 'parent', "Children",
        domain=[
            ('company', '=', Eval('company', -1)),
            ])
    balance = fields.Function(Monetary(
            "Balance", currency='currency', digits='currency'),
        'get_balance')
    credit = fields.Function(Monetary(
            "Credit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_credit_debit')
    debit = fields.Function(Monetary(
            "Debit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_credit_debit')
    amount_second_currency = fields.Function(Monetary(
            "Amount Second Currency",
            currency='second_currency', digits='second_currency',
            states={
                'invisible': ~Eval('second_currency'),
                }),
        'get_credit_debit')
    line_count = fields.Function(
        fields.Integer("Line Count"), 'get_credit_debit')
    note = fields.Text('Note')
    deferrals = fields.One2Many(
        'account.account.deferral', 'account', "Deferrals", readonly=True,
        states={
            'invisible': ~Eval('type'),
            })
    taxes = fields.Many2Many('account.account-account.tax',
        'account', 'tax', 'Default Taxes',
        domain=[
            ('company', '=', Eval('company')),
            ('parent', '=', None),
            ],
        help="Default tax for manual encoding of move lines\n"
        'for journal types: "expense" and "revenue".')
    replaced_by = fields.Many2One(
        'account.account', "Replaced By",
        domain=[('company', '=', Eval('company', -1))],
        states={
            'readonly': _states['readonly'],
            'invisible': ~Eval('end_date'),
            })
    template = fields.Many2One('account.account.template', 'Template')
    template_override = fields.Boolean('Override Template',
        help="Check to override template definition",
        states={
            'invisible': ~Bool(Eval('template', -1)),
            })
    del _states

    @classmethod
    def __setup__(cls):
        super(Account, cls).__setup__()
        for date in [cls.start_date, cls.end_date]:
            date.states = {
                'readonly': (Bool(Eval('template', -1))
                    & ~Eval('template_override', False)),
                }
        cls._order.insert(0, ('code', 'ASC'))
        cls._order.insert(1, ('name', 'ASC'))
        table = cls.__table__()
        cls._sql_constraints.append(
            ('only_one_debit_credit_types', Check(
                    table, (table.debit_type + table.credit_type) == Null),
                'account.msg_only_one_debit_credit_types'))
        cls._sql_indexes.add(
            Index(
                table,
                (table.left, Index.Range()),
                (table.right, Index.Range())))

    @classmethod
    def __register__(cls, module_name):
        super().__register__(module_name)

        table_h = cls.__table_handler__(module_name)

        # Migration from 5.0: remove kind
        table_h.drop_column('kind')

    @classmethod
    def validate_fields(cls, accounts, field_names):
        super().validate_fields(accounts, field_names)
        cls.check_second_currency(accounts, field_names)
        cls.check_move_domain(accounts, field_names)

    @staticmethod
    def default_left():
        return 0

    @staticmethod
    def default_right():
        return 0

    @staticmethod
    def default_company():
        return Transaction().context.get('company') or None

    @classmethod
    def default_template_override(cls):
        return False

    def get_currency(self, name):
        return self.company.currency.id

    @classmethod
    def get_balance(cls, accounts, name):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        FiscalYear = pool.get('account.fiscalyear')
        cursor = Transaction().connection.cursor()

        table_a = cls.__table__()
        table_c = cls.__table__()
        line = MoveLine.__table__()
        ids = [a.id for a in accounts]
        balances = dict((i, Decimal(0)) for i in ids)
        line_query, fiscalyear_ids = MoveLine.query_get(line)
        for sub_ids in grouped_slice(ids):
            red_sql = reduce_ids(table_a.id, sub_ids)
            cursor.execute(*table_a.join(table_c,
                    condition=(table_c.left >= table_a.left)
                    & (table_c.right <= table_a.right)
                    ).join(line, condition=line.account == table_c.id
                    ).select(
                    table_a.id,
                    Sum(Coalesce(line.debit, 0) - Coalesce(line.credit, 0)),
                    where=red_sql & line_query,
                    group_by=table_a.id))
            balances.update(dict(cursor))

        for account in accounts:
            # SQLite uses float for SUM
            if not isinstance(balances[account.id], Decimal):
                balances[account.id] = Decimal(str(balances[account.id]))
            balances[account.id] = account.currency.round(balances[account.id])

        fiscalyears = FiscalYear.browse(fiscalyear_ids)

        def func(accounts, names):
            return {names[0]: cls.get_balance(accounts, names[0])}
        return cls._cumulate(fiscalyears, accounts, [name], {name: balances},
            func)[name]

    @classmethod
    def get_credit_debit(cls, accounts, names):
        '''
        Function to compute debit, credit for accounts.
        If cumulate is set in the context, it is the cumulate amount over all
        previous fiscal year.
        '''
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        FiscalYear = pool.get('account.fiscalyear')
        cursor = Transaction().connection.cursor()

        result = {}
        ids = [a.id for a in accounts]
        for name in names:
            if name not in {
                    'credit', 'debit', 'amount_second_currency', 'line_count'}:
                raise ValueError('Unknown name: %s' % name)
            col_type = int if name == 'line_count' else Decimal
            result[name] = defaultdict(col_type)

        table = cls.__table__()
        line = MoveLine.__table__()
        line_query, fiscalyear_ids = MoveLine.query_get(line)
        columns = [table.id]
        for name in names:
            if name == 'line_count':
                columns.append(Count(Literal('*')))
            else:
                columns.append(Sum(Coalesce(Column(line, name), 0)))
        for sub_ids in grouped_slice(ids):
            red_sql = reduce_ids(table.id, sub_ids)
            cursor.execute(*table.join(line, 'LEFT',
                    condition=line.account == table.id
                    ).select(*columns,
                    where=red_sql & line_query,
                    group_by=table.id))
            for row in cursor:
                account_id = row[0]
                for i, name in enumerate(names, 1):
                    # SQLite uses float for SUM
                    if (name != 'line_count'
                            and not isinstance(row[i], Decimal)):
                        result[name][account_id] = Decimal(str(row[i]))
                    else:
                        result[name][account_id] = row[i]
        for account in accounts:
            for name in names:
                if name == 'line_count':
                    continue
                if (name == 'amount_second_currency'
                        and account.second_currency):
                    currency = account.second_currency
                else:
                    currency = account.currency
                result[name][account.id] = currency.round(
                    result[name][account.id])

        cumulate_names = []
        if Transaction().context.get('cumulate'):
            cumulate_names = names
        elif 'amount_second_currency' in names:
            cumulate_names = ['amount_second_currency']
        if cumulate_names:
            fiscalyears = FiscalYear.browse(fiscalyear_ids)
            return cls._cumulate(fiscalyears, accounts, cumulate_names, result,
                cls.get_credit_debit)
        else:
            return result

    @classmethod
    def _cumulate(
            cls, fiscalyears, accounts, names, values, func,
            deferral='account.account.deferral'):
        """
        Cumulate previous fiscalyear values into values
        func is the method to compute values
        """
        pool = Pool()
        FiscalYear = pool.get('account.fiscalyear')

        youngest_fiscalyear = None
        for fiscalyear in fiscalyears:
            if (not youngest_fiscalyear
                    or (youngest_fiscalyear.start_date
                        > fiscalyear.start_date)):
                youngest_fiscalyear = fiscalyear

        fiscalyear = None
        if youngest_fiscalyear:
            fiscalyears = FiscalYear.search([
                    ('end_date', '<', youngest_fiscalyear.start_date),
                    ('company', '=', youngest_fiscalyear.company),
                    ], order=[('end_date', 'DESC')], limit=1)
            if fiscalyears:
                fiscalyear, = fiscalyears

        if not fiscalyear:
            return values

        if fiscalyear.state == 'close' and deferral:
            Deferral = pool.get(deferral)
            id2deferral = {}
            ids = [a.id for a in accounts]
            for sub_ids in grouped_slice(ids):
                deferrals = Deferral.search([
                    ('fiscalyear', '=', fiscalyear.id),
                    ('account', 'in', list(sub_ids)),
                    ])
                for deferral in deferrals:
                    id2deferral[deferral.account.id] = deferral

            for account in accounts:
                if account.id in id2deferral:
                    deferral = id2deferral[account.id]
                    for name in names:
                        values[name][account.id] += getattr(deferral, name)
        else:
            with Transaction().set_context(fiscalyear=fiscalyear.id,
                    date=None, periods=None, from_date=None, to_date=None):
                previous_result = func(accounts, names)
            for name in names:
                vals = values[name]
                for account in accounts:
                    vals[account.id] += previous_result[name][account.id]

        return values

    __on_change_parent_fields = ['name', 'code', 'company', 'type',
        'debit_type', 'credit_type', 'reconcile', 'deferral', 'party_required',
        'general_ledger_balance', 'taxes']

    @fields.depends('parent', *(__on_change_parent_fields
            + ['_parent_parent.%s' % f for f in __on_change_parent_fields]))
    def on_change_parent(self):
        if not self.parent:
            return
        for field in self.__on_change_parent_fields:
            if (not getattr(self, field)
                    or field in {'reconcile', 'deferral',
                        'party_required', 'general_ledger_balance'}):
                setattr(self, field, getattr(self.parent, field))

    @classmethod
    def check_second_currency(cls, accounts, field_names=None):
        pool = Pool()
        Line = pool.get('account.move.line')
        if field_names and not (
                field_names & (
                    {'second_currency', 'type'}
                    | set(cls.deferral.validation_depends))):
            return
        for account in accounts:
            if not account.second_currency:
                continue
            if (not account.type
                    or account.type.payable
                    or account.type.revenue
                    or account.type.receivable
                    or account.type.expense):
                raise SecondCurrencyError(
                    gettext('account.msg_account_invalid_type_second_currency',
                        account=account.rec_name))
            if not account.deferral:
                raise SecondCurrencyError(
                    gettext('account'
                        '.msg_account_invalid_deferral_second_currency',
                        account=account.rec_name))
            lines = Line.search([
                    ('account', '=', account.id),
                    ('second_currency', '!=', account.second_currency.id),
                    ], order=[], limit=1)
            if lines:
                raise SecondCurrencyError(
                    gettext('account'
                        '.msg_account_invalid_lines_second_currency',
                        currency=account.second_currency.rec_name,
                        account=account.rec_name))

    @classmethod
    def check_move_domain(cls, accounts, field_names=None):
        pool = Pool()
        Line = pool.get('account.move.line')
        if field_names and not (field_names & {'closed', 'type'}):
            return
        accounts = [a for a in accounts if a.closed or not a.type]
        for sub_accounts in grouped_slice(accounts):
            sub_accounts = list(sub_accounts)
            if Line.search([
                        ('account', 'in', [a.id for a in sub_accounts]),
                        ], order=[], limit=1):
                for account in sub_accounts:
                    if not account.closed:
                        continue
                    lines = Line.search([
                            ('account', '=', account.id),
                            ], order=[], limit=1)
                    if lines:
                        if account.closed:
                            raise AccountValidationError(gettext(
                                    'account.msg_account_closed_lines',
                                    account=account.rec_name))
                        elif account.type:
                            raise AccountValidationError(gettext(
                                    'account.msg_account_no_type_lines',
                                    account=account.rec_name))

    @classmethod
    def copy(cls, accounts, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('template', None)
        default.setdefault('deferrals', [])
        new_accounts = super(Account, cls).copy(accounts, default=default)
        cls._rebuild_tree('parent', None, 0)
        return new_accounts

    @classmethod
    def delete(cls, accounts):
        MoveLine = Pool().get('account.move.line')
        childs = cls.search([
                ('parent', 'child_of', [a.id for a in accounts]),
                ])
        lines = MoveLine.search([
                ('account', 'in', [a.id for a in childs]),
                ])
        if lines:
            raise AccessError(
                gettext('account.msg_delete_account_with_move_lines',
                    account=lines[0].account.rec_name))
        super(Account, cls).delete(accounts)

    def update_account(self, template2account=None, template2type=None):
        '''
        Update recursively accounts based on template.
        template2account is a dictionary with template id as key and account id
        as value, used to convert template id into account. The dictionary is
        filled with new accounts.
        template2type is a dictionary with type template id as key and type id
        as value, used to convert type template id into type.
        '''
        if template2account is None:
            template2account = {}

        if template2type is None:
            template2type = {}

        values = []
        childs = [self]
        while childs:
            for child in childs:
                if child.template:
                    if not child.template_override:
                        vals = child.template._get_account_value(account=child)
                        current_type = child.type.id if child.type else None
                        if child.template.type:
                            template_type = template2type.get(
                                child.template.type.id)
                        else:
                            template_type = None
                        if current_type != template_type:
                            vals['type'] = template_type
                        current_debit_type = (
                            child.debit_type.id if child.debit_type else None)
                        if child.template.debit_type:
                            template_debit_type = template2type.get(
                                child.template.debit_type.id)
                        else:
                            template_debit_type = None
                        if current_debit_type != template_debit_type:
                            vals['debit_type'] = template_debit_type
                        current_credit_type = (
                            child.credit_type.id if child.credit_type
                            else None)
                        if child.template.credit_type:
                            template_credit_type = template2type.get(
                                child.template.credit_type.id)
                        else:
                            template_credit_type = None
                        if current_credit_type != template_credit_type:
                            vals['credit_type'] = template_credit_type
                        if vals:
                            values.append([child])
                            values.append(vals)
                    template2account[child.template.id] = child.id
            childs = sum((c.childs for c in childs), ())
        if values:
            self.write(*values)

    def update_account2(self, template2account, template2tax):
        '''
        Update recursively account taxes and replaced_by base on template.
        template2account is a dictionary with template id as key and account id
        as value, used to convert template id into account.
        template2tax is a dictionary with tax template id as key and tax id as
        value, used to convert tax template id into tax.
        '''
        if template2account is None:
            template2account = {}

        if template2tax is None:
            template2tax = {}

        to_write = []
        childs = [self]
        while childs:
            for child in childs:
                if not child.template:
                    continue
                if not child.template.taxes:
                    continue
                values = {}
                tax_ids = [template2tax[x.id] for x in child.template.taxes
                    if x.id in template2tax]
                old_tax_ids = [x.id for x in child.taxes]
                for tax_id in tax_ids:
                    if tax_id not in old_tax_ids:
                        values['taxes'] = [
                            ('add', [template2tax[x.id]
                                    for x in child.template.taxes
                                    if x.id in template2tax])]
                        break
                if child.template.parent:
                    parent = template2account[child.template.parent.id]
                else:
                    parent = None
                old_parent = child.parent.id if child.parent else None
                if parent != old_parent:
                    values['parent'] = parent
                if child.template.replaced_by:
                    replaced_by = template2account[
                        child.template.replaced_by.id]
                else:
                    replaced_by = None
                old_replaced_by = (
                    child.replaced_by.id if child.replaced_by else None)
                if old_replaced_by != replaced_by:
                    values['replaced_by'] = replaced_by
                if values:
                    to_write.append([child])
                    to_write.append(values)
            childs = sum((c.childs for c in childs), ())
        if to_write:
            self.write(*to_write)

    def current(self, date=None):
        "Return the actual account for the date"
        pool = Pool()
        Date = pool.get('ir.date')
        context = Transaction().context
        if date is None:
            with Transaction().set_context(company=self.company.id):
                date = context.get('date') or Date.today()
        if self.start_date and date < self.start_date:
            return None
        elif self.end_date and self.end_date < date:
            if self.replaced_by:
                return self.replaced_by.current(date=date)
            else:
                return None
        else:
            return self


class AccountParty(ActivePeriodMixin, ModelSQL):
    "Account Party"
    __name__ = 'account.account.party'
    account = fields.Many2One('account.account', "Account")
    party = fields.Many2One(
        'party.party', "Party",
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})
    name = fields.Char("Name")
    code = fields.Char("Code")
    company = fields.Many2One('company.company', "Company")
    type = fields.Many2One('account.account.type', "Type")
    debit_type = fields.Many2One('account.account.type', "Debit Type")
    credit_type = fields.Many2One('account.account.type', "Credit Type")
    closed = fields.Boolean("Closed")

    balance = fields.Function(Monetary(
            "Balance", currency='currency', digits='currency'),
        'get_balance')
    credit = fields.Function(Monetary(
            "Credit", currency='currency', digits='currency'),
        'get_credit_debit')
    debit = fields.Function(Monetary(
            "Debit", currency='currency', digits='currency'),
        'get_credit_debit')
    amount_second_currency = fields.Function(Monetary(
            "Amount Second Currency",
            currency='second_currency', digits='second_currency',
            states={
                'invisible': ~Eval('second_currency'),
                }),
        'get_credit_debit')
    line_count = fields.Function(
        fields.Integer("Line Count"), 'get_credit_debit')
    second_currency = fields.Many2One(
        'currency.currency', "Secondary Currency")

    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"), 'get_currency')

    @classmethod
    def table_query(cls):
        pool = Pool()
        Line = pool.get('account.move.line')
        Account = pool.get('account.account')
        line = Line.__table__()
        account = Account.__table__()

        account_party = line.select(
                Min(line.id).as_('id'), line.account, line.party,
                where=line.party != Null,
                group_by=[line.account, line.party])

        columns = []
        for fname, field in cls._fields.items():
            if not hasattr(field, 'set'):
                if fname in {'id', 'account', 'party'}:
                    column = Column(account_party, fname)
                else:
                    column = Column(account, fname)
                columns.append(column.as_(fname))
        return (
            account_party.join(
                account, condition=account_party.account == account.id)
            .select(
                *columns,
                where=account.party_required))

    @classmethod
    def get_balance(cls, records, name):
        pool = Pool()
        Account = pool.get('account.account')
        MoveLine = pool.get('account.move.line')
        FiscalYear = pool.get('account.fiscalyear')
        cursor = Transaction().connection.cursor()

        table_a = Account.__table__()
        table_c = Account.__table__()
        line = MoveLine.__table__()
        ids = [a.id for a in records]
        account_ids = {a.account.id for a in records}
        party_ids = {a.party.id for a in records}
        account_party2id = {(a.account.id, a.party.id): a.id for a in records}
        balances = dict((i, Decimal(0)) for i in ids)
        line_query, fiscalyear_ids = MoveLine.query_get(line)
        for sub_account_ids in grouped_slice(account_ids):
            account_sql = reduce_ids(table_a.id, sub_account_ids)
            for sub_party_ids in grouped_slice(party_ids):
                party_sql = reduce_ids(line.party, sub_party_ids)
                cursor.execute(*table_a.join(table_c,
                        condition=(table_c.left >= table_a.left)
                        & (table_c.right <= table_a.right)
                        ).join(line, condition=line.account == table_c.id
                        ).select(
                        table_a.id,
                        line.party,
                        Sum(
                            Coalesce(line.debit, 0)
                            - Coalesce(line.credit, 0)),
                        where=account_sql & party_sql & line_query,
                        group_by=[table_a.id, line.party]))
                for account_id, party_id, balance in cursor:
                    try:
                        id_ = account_party2id[(account_id, party_id)]
                    except KeyError:
                        # There can be more combinations of account-party in
                        # the database than from records
                        continue
                    balances[id_] = balance

        for record in records:
            # SQLite uses float for SUM
            if not isinstance(balances[record.id], Decimal):
                balances[record.id] = Decimal(str(balances[record.id]))
            balances[record.id] = record.currency.round(balances[record.id])

        fiscalyears = FiscalYear.browse(fiscalyear_ids)

        def func(records, names):
            return {names[0]: cls.get_balance(records, names[0])}
        return Account._cumulate(
            fiscalyears, records, [name], {name: balances}, func,
            deferral=None)[name]

    @classmethod
    def get_credit_debit(cls, records, names):
        pool = Pool()
        Account = pool.get('account.account')
        MoveLine = pool.get('account.move.line')
        FiscalYear = pool.get('account.fiscalyear')
        cursor = Transaction().connection.cursor()

        result = {}
        for name in names:
            if name not in {
                    'credit', 'debit', 'amount_second_currency', 'line_count'}:
                raise ValueError('Unknown name: %s' % name)
            column_type = int if name == 'line_count' else Decimal
            result[name] = defaultdict(column_type)

        account_ids = {a.account.id for a in records}
        party_ids = {a.party.id for a in records}
        account_party2id = {(a.account.id, a.party.id): a.id for a in records}
        table = Account.__table__()
        line = MoveLine.__table__()
        line_query, fiscalyear_ids = MoveLine.query_get(line)
        columns = [table.id, line.party]
        for name in names:
            if name == 'line_count':
                columns.append(Count(Literal('*')))
            else:
                columns.append(Sum(Coalesce(Column(line, name), 0)))
        for sub_account_ids in grouped_slice(account_ids):
            account_sql = reduce_ids(table.id, sub_account_ids)
            for sub_party_ids in grouped_slice(party_ids):
                party_sql = reduce_ids(line.party, sub_party_ids)
                cursor.execute(*table.join(line, 'LEFT',
                        condition=line.account == table.id
                        ).select(*columns,
                        where=account_sql & party_sql & line_query,
                        group_by=[table.id, line.party]))
                for row in cursor:
                    try:
                        id_ = account_party2id[tuple(row[0:2])]
                    except KeyError:
                        # There can be more combinations of account-party in
                        # the database than from records
                        continue
                    for i, name in enumerate(names, 2):
                        # SQLite uses float for SUM
                        if (name != 'line_count'
                                and not isinstance(row[i], Decimal)):
                            result[name][id_] = Decimal(str(row[i]))
                        else:
                            result[name][id_] = row[i]
        for record in records:
            for name in names:
                if name == 'line_count':
                    continue
                if name == 'amount_second_currency' and record.second_currency:
                    currency = record.second_currency
                else:
                    currency = record.currency
                result[name][record.id] = currency.round(
                    result[name][record.id])

        cumulate_names = []
        if Transaction().context.get('cumulate'):
            cumulate_names = names
        elif 'amount_second_currency' in names:
            cumulate_names = ['amount_second_currency']
        if cumulate_names:
            fiscalyears = FiscalYear.browse(fiscalyear_ids)
            return Account._cumulate(
                fiscalyears, records, cumulate_names, result,
                cls.get_credit_debit, deferral=None)
        else:
            return result

    def get_currency(self, name):
        return self.company.currency.id


class AccountDeferral(ModelSQL, ModelView):
    '''
    Account Deferral

    It is used to deferral the debit/credit of account by fiscal year.
    '''
    __name__ = 'account.account.deferral'
    account = fields.Many2One('account.account', "Account", required=True)
    fiscalyear = fields.Many2One(
        'account.fiscalyear', "Fiscal Year", required=True)
    debit = Monetary(
        "Debit", currency='currency', digits='currency', required=True)
    credit = Monetary(
        "Credit", currency='currency', digits='currency', required=True)
    balance = fields.Function(Monetary(
            "Balance", currency='currency', digits='currency'),
        'get_balance')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"), 'get_currency')
    amount_second_currency = Monetary(
        "Amount Second Currency",
        currency='second_currency', digits='second_currency', required=True,
        states={
            'invisible': ~Eval('second_currency'),
            })
    line_count = fields.Integer("Line Count", required=True)
    second_currency = fields.Function(fields.Many2One(
            'currency.currency', "Second Currency"), 'get_second_currency')

    @classmethod
    def __setup__(cls):
        super(AccountDeferral, cls).__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('deferral_uniq', Unique(t, t.account, t.fiscalyear),
                'account.msg_deferral_unique'),
        ]
        cls._sql_indexes.add(
            Index(
                t,
                (t.fiscalyear, Index.Equality()),
                (t.account, Index.Equality())))

    @classmethod
    def __register__(cls, module_name):
        pool = Pool()
        MoveLine = pool.get('account.move.line')
        Move = pool.get('account.move')
        Period = pool.get('account.period')

        cursor = Transaction().connection.cursor()
        deferral = cls.__table__()
        move_line = MoveLine.__table__()
        move = Move.__table__()
        period = Period.__table__()

        exist = backend.TableHandler.table_exist(cls._table)
        table_h = cls.__table_handler__(module_name)
        created_line_count = exist and not table_h.column_exist('line_count')

        super().__register__(module_name)

        # Migration from 6.2: add line_count on deferrals
        if created_line_count:
            counting_query = (move_line
                .join(move, condition=move_line.move == move.id)
                .join(period, condition=move.period == period.id)
                .select(
                    move_line.account, period.fiscalyear,
                    Count(Literal('*')).as_('line_count'),
                    group_by=[move_line.account, period.fiscalyear]))
            cursor.execute(*deferral.update(
                    [deferral.line_count], [counting_query.line_count],
                    from_=[counting_query],
                    where=((deferral.account == counting_query.account)
                        & (deferral.fiscalyear == counting_query.fiscalyear))))

    @classmethod
    def default_amount_second_currency(cls):
        return Decimal(0)

    @classmethod
    def default_line_count(cls):
        return 0

    @classmethod
    def get_balance(cls, deferrals, name):
        pool = Pool()
        Account = pool.get('account.account')
        cursor = Transaction().connection.cursor()

        table = cls.__table__()
        table_child = cls.__table__()
        account = Account.__table__()
        account_child = Account.__table__()
        balances = defaultdict(Decimal)

        for sub_deferrals in grouped_slice(deferrals):
            red_sql = reduce_ids(table.id, [d.id for d in sub_deferrals])
            cursor.execute(*table
                .join(account, condition=table.account == account.id)
                .join(account_child,
                    condition=(account_child.left >= account.left)
                    & (account_child.right <= account.right))
                .join(table_child,
                    condition=(table_child.account == account_child.id)
                    & (table_child.fiscalyear == table.fiscalyear))
                .select(
                    table.id,
                    Sum(table_child.debit - table_child.credit),
                    where=red_sql,
                    group_by=table.id))
            balances.update(dict(cursor))

        for id_, balance in balances.items():
            if not isinstance(balance, Decimal):
                balances[id_] = Decimal(str(balance))
        return balances

    def get_currency(self, name):
        return self.account.currency.id

    def get_second_currency(self, name):
        if self.account.second_currency:
            return self.account.second_currency.id

    def get_rec_name(self, name):
        return '%s - %s' % (self.account.rec_name, self.fiscalyear.rec_name)

    @classmethod
    def search_rec_name(cls, name, clause):
        if clause[1].startswith('!') or clause[1].startswith('not '):
            bool_op = 'AND'
        else:
            bool_op = 'OR'
        return [bool_op,
            ('account.rec_name',) + tuple(clause[1:]),
            ('fiscalyear.rec_name',) + tuple(clause[1:]),
            ]

    @classmethod
    def write(cls, deferrals, values, *args):
        raise AccessError(gettext('account.msg_write_deferral'))


class AccountTax(ModelSQL):
    'Account - Tax'
    __name__ = 'account.account-account.tax'
    _table = 'account_account_tax_rel'
    account = fields.Many2One(
        'account.account', "Account", ondelete='CASCADE', required=True)
    tax = fields.Many2One(
        'account.tax', "Tax", ondelete='RESTRICT', required=True)


class AccountContext(ModelView):
    'Account Context'
    __name__ = 'account.account.context'

    company = fields.Many2One('company.company', "Company", required=True)
    fiscalyear = fields.Many2One(
        'account.fiscalyear', "Fiscal Year",
        domain=[
            ('company', '=', Eval('company', -1)),
            ],
        help="Leave empty for all open fiscal year.")
    posted = fields.Boolean('Posted Moves', help="Only include posted moves.")

    @classmethod
    def default_company(cls):
        return Transaction().context.get('company')

    @fields.depends('company', 'fiscalyear')
    def on_change_company(self):
        if self.fiscalyear and self.fiscalyear.company != self.company:
            self.fiscalyear = None

    @classmethod
    def default_posted(cls):
        return False


class _GeneralLedgerAccount(ActivePeriodMixin, ModelSQL, ModelView):

    account = fields.Many2One('account.account', "Account")
    company = fields.Many2One('company.company', 'Company')
    start_debit = fields.Function(Monetary(
            "Start Debit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_account', searcher='search_account')
    debit = fields.Function(Monetary(
            "Debit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_debit_credit', searcher='search_debit_credit')
    end_debit = fields.Function(Monetary(
            "End Debit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_account', searcher='search_account')
    start_credit = fields.Function(Monetary(
            "Start Credit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_account', searcher='search_account')
    credit = fields.Function(Monetary(
            "Credit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_debit_credit', searcher='search_debit_credit')
    end_credit = fields.Function(Monetary(
            "End Credit", currency='currency', digits='currency',
            states={
                'invisible': ~Eval('line_count', -1),
                }),
        'get_account', searcher='search_account')
    line_count = fields.Function(
        fields.Integer("Line Count"),
        'get_debit_credit', searcher='search_debit_credit')
    start_balance = fields.Function(Monetary(
            "Start Balance", currency='currency', digits='currency'),
        'get_account', searcher='search_account')
    end_balance = fields.Function(Monetary(
            "End Balance", currency='currency', digits='currency'),
        'get_account', searcher='search_account')
    currency = fields.Function(fields.Many2One(
        'currency.currency', 'Currency'), 'get_currency')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(0, ('account', 'ASC'))

    @classmethod
    def table_query(cls):
        context = Transaction().context
        Account = cls._get_account()
        account = Account.__table__()
        columns = []
        for fname, field in cls._fields.items():
            if not hasattr(field, 'set'):
                if (isinstance(field, fields.Many2One)
                        and field.get_target() == Account):
                    column = Column(account, 'id')
                else:
                    column = Column(account, fname)
                columns.append(column.as_(fname))
        return account.select(*columns,
            where=(account.company == context.get('company'))
            & (account.type != Null)
            & (account.closed != Literal(True)))

    @classmethod
    def get_period_ids(cls, name):
        pool = Pool()
        Period = pool.get('account.period')
        context = Transaction().context

        period = None
        if name.startswith('start_'):
            period_ids = []
            if context.get('start_period'):
                period = Period(context['start_period'])
        elif name.startswith('end_'):
            period_ids = []
            if context.get('end_period'):
                period = Period(context['end_period'])
            else:
                periods = Period.search([
                        ('fiscalyear', '=', context.get('fiscalyear')),
                        ('type', '=', 'standard'),
                        ],
                    order=[('start_date', 'DESC')], limit=1)
                if periods:
                    period, = periods

        if period:
            if name.startswith('start_'):
                date_clause = ('end_date', '<=', period.start_date)
            else:
                date_clause = [
                    ('end_date', '<=', period.end_date),
                    ('start_date', '<', period.end_date),
                    ]
            periods = Period.search([
                    ('fiscalyear', '=', context.get('fiscalyear')),
                    date_clause,
                    ])
            if period.start_date == period.end_date:
                periods.append(period)
            if periods:
                period_ids = [p.id for p in periods]
            if name.startswith('end_'):
                # Always include ending period
                period_ids.append(period.id)
        return period_ids

    @classmethod
    def get_dates(cls, name):
        context = Transaction().context
        if name.startswith('start_'):
            from_date = context.get('from_date')
            if from_date:
                from_date -= datetime.timedelta(days=1)
            return None, from_date
        elif name.startswith('end_'):
            return None, context.get('to_date')
        return None, None

    @classmethod
    def get_account(cls, records, name):
        Account = cls._get_account()

        period_ids, from_date, to_date = None, None, None
        context = Transaction().context
        if context.get('start_period') or context.get('end_period'):
            period_ids = cls.get_period_ids(name)
        elif context.get('from_date') or context.get('end_date'):
            from_date, to_date = cls.get_dates(name)
        else:
            if name.startswith('start_'):
                period_ids = []

        with Transaction().set_context(
                periods=period_ids,
                from_date=from_date, to_date=to_date):
            accounts = Account.browse(records)
        fname = name
        for test in ['start_', 'end_']:
            if name.startswith(test):
                fname = name[len(test):]
                break
        return {a.id: getattr(a, fname) for a in accounts}

    @classmethod
    def search_account(cls, name, domain):
        Account = cls._get_account()

        period_ids = cls.get_period_ids(name)
        with Transaction().set_context(periods=period_ids):
            accounts = Account.search([], order=[])

        _, operator_, operand = domain
        operator_ = {
            '=': operator.eq,
            '>=': operator.ge,
            '>': operator.gt,
            '<=': operator.le,
            '<': operator.lt,
            '!=': operator.ne,
            'in': lambda v, l: v in l,
            'not in': lambda v, l: v not in l,
            }.get(operator_, lambda v, l: False)
        fname = name
        for test in ['start_', 'end_']:
            if name.startswith(test):
                fname = name[len(test):]
                break

        ids = [a.id for a in accounts
            if operand is not None and operator_(getattr(a, fname), operand)]
        return [('id', 'in', ids)]

    @classmethod
    def _debit_credit_context(cls):
        period_ids, from_date, to_date = None, None, None
        context = Transaction().context
        if context.get('start_period') or context.get('end_period'):
            start_period_ids = set(cls.get_period_ids('start_balance'))
            end_period_ids = set(cls.get_period_ids('end_balance'))
            period_ids = list(end_period_ids.difference(start_period_ids))
        elif context.get('from_date') or context.get('to_date'):
            from_date = context.get('from_date')
            to_date = context.get('to_date')
        return {
            'periods': period_ids,
            'from_date': from_date,
            'to_date': to_date,
            }

    @classmethod
    def get_debit_credit(cls, records, name):
        Account = cls._get_account()

        with Transaction().set_context(cls._debit_credit_context()):
            accounts = Account.browse(records)
        return {a.id: getattr(a, name) for a in accounts}

    @classmethod
    def search_debit_credit(cls, name, domain):
        Account = cls._get_account()

        with Transaction().set_context(cls._debit_credit_context()):
            accounts = Account.search([], order=[])

        _, operator_, operand = domain
        operator_ = {
            '=': operator.eq,
            '>=': operator.ge,
            '>': operator.gt,
            '<=': operator.le,
            '<': operator.lt,
            '!=': operator.ne,
            'in': lambda v, l: v in l,
            'not in': lambda v, l: v not in l,
            }.get(operator_, lambda v, l: False)

        ids = [a.id for a in accounts
            if operand is not None and operator_(getattr(a, name), operand)]
        return [('id', 'in', ids)]

    def get_currency(self, name):
        return self.company.currency.id

    def get_rec_name(self, name):
        return self.account.rec_name

    @classmethod
    def search_rec_name(cls, name, clause):
        return [('account.rec_name',) + tuple(clause[1:])]


class GeneralLedgerAccount(_GeneralLedgerAccount):
    'General Ledger Account'
    __name__ = 'account.general_ledger.account'

    type = fields.Many2One('account.account.type', "Type")
    debit_type = fields.Many2One('account.account.type', "Debit Type")
    credit_type = fields.Many2One('account.account.type', "Credit Type")
    lines = fields.One2Many(
        'account.general_ledger.line', 'account', "Lines", readonly=True)
    general_ledger_balance = fields.Boolean("General Ledger Balance")

    @classmethod
    def _get_account(cls):
        pool = Pool()
        return pool.get('account.account')


class GeneralLedgerAccountContext(ModelView):
    'General Ledger Account Context'
    __name__ = 'account.general_ledger.account.context'
    fiscalyear = fields.Many2One('account.fiscalyear', 'Fiscal Year',
        required=True,
        domain=[
            ('company', '=', Eval('company')),
            ],
        depends=['company'])
    start_period = fields.Many2One('account.period', 'Start Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear')),
            ('start_date', '<=', (Eval('end_period'), 'start_date')),
            ],
        states={
            'invisible': Eval('from_date', False) | Eval('to_date', False),
            })
    end_period = fields.Many2One('account.period', 'End Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear')),
            ('start_date', '>=', (Eval('start_period'), 'start_date'))
            ],
        states={
            'invisible': Eval('from_date', False) | Eval('to_date', False),
            })
    from_date = fields.Date("From Date",
        domain=[
            If(Eval('to_date') & Eval('from_date'),
                ('from_date', '<=', Eval('to_date')),
                ()),
            ],
        states={
            'invisible': (Eval('start_period', False)
                | Eval('end_period', False)),
            })
    to_date = fields.Date("To Date",
        domain=[
            If(Eval('from_date') & Eval('to_date'),
                ('to_date', '>=', Eval('from_date')),
                ()),
            ],
        states={
            'invisible': (Eval('start_period', False)
                | Eval('end_period', False)),
            })
    company = fields.Many2One('company.company', 'Company', required=True)
    posted = fields.Boolean('Posted Move', help="Only include posted moves.")
    journal = fields.Many2One(
        'account.journal', "Journal",
        context={
            'company': Eval('company', -1),
            },
        depends={'company'},
        help="Only include moves from the journal.")

    @classmethod
    def default_fiscalyear(cls):
        pool = Pool()
        FiscalYear = pool.get('account.fiscalyear')
        context = Transaction().context
        return context.get(
            'fiscalyear',
            FiscalYear.find(context.get('company'), exception=False))

    @classmethod
    def default_start_period(cls):
        return Transaction().context.get('start_period')

    @classmethod
    def default_end_period(cls):
        return Transaction().context.get('end_period')

    @classmethod
    def default_company(cls):
        return Transaction().context.get('company')

    @classmethod
    def default_posted(cls):
        return Transaction().context.get('posted', False)

    @classmethod
    def default_journal(cls):
        return Transaction().context.get('journal')

    @classmethod
    def default_from_date(cls):
        return Transaction().context.get('from_date')

    @classmethod
    def default_to_date(cls):
        return Transaction().context.get('to_date')

    @fields.depends('company', 'fiscalyear', methods=['on_change_fiscalyear'])
    def on_change_company(self):
        if self.fiscalyear and self.fiscalyear.company != self.company:
            self.fiscalyear = None
            self.on_change_fiscalyear()

    @fields.depends('fiscalyear', 'start_period', 'end_period')
    def on_change_fiscalyear(self):
        if (self.start_period
                and self.start_period.fiscalyear != self.fiscalyear):
            self.start_period = None
        if (self.end_period
                and self.end_period.fiscalyear != self.fiscalyear):
            self.end_period = None

    @fields.depends('start_period')
    def on_change_start_period(self):
        if self.start_period:
            self.from_date = self.to_date = None

    @fields.depends('end_period')
    def on_change_end_period(self):
        if self.end_period:
            self.from_date = self.to_date = None

    @fields.depends('from_date')
    def on_change_from_date(self):
        if self.from_date:
            self.start_period = self.end_period = None

    @fields.depends('to_date')
    def on_change_to_date(self):
        if self.to_date:
            self.start_period = self.end_period = None


class GeneralLedgerAccountParty(_GeneralLedgerAccount):
    "General Ledger Account Party"
    __name__ = 'account.general_ledger.account.party'

    party = fields.Many2One(
        'party.party', "Party",
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order.insert(2, ('party', 'ASC'))

    @classmethod
    def _get_account(cls):
        pool = Pool()
        return pool.get('account.account.party')

    def get_rec_name(self, name):
        return ' - '.join((self.account.rec_name, self.party.rec_name))

    @classmethod
    def search_rec_name(cls, name, clause):
        if clause[1].startswith('!') or clause[1].startswith('not '):
            bool_op = 'AND'
        else:
            bool_op = 'OR'
        return [bool_op,
            ('account.rec_name',) + tuple(clause[1:]),
            ('party.rec_name',) + tuple(clause[1:]),
            ]


class OpenGeneralLedgerAccountParty(Wizard):
    "Open General Ledger Account Party"
    __name__ = 'account.general_ledger.account.party.open'
    start_state = 'open'
    open = StateAction('account.act_general_ledger_line_form')

    def do_open(self, action):
        action['name'] = '%s (%s)' % (action['name'], self.record.rec_name)
        domain = [
            ('account', '=', self.record.account.id),
            ('party', '=', self.record.party.id),
            ]
        action['pyson_domain'] = PYSONEncoder().encode(domain)
        action['context_model'] = 'account.general_ledger.account.context'
        action['pyson_context'] = PYSONEncoder().encode({
                'party_cumulate': True,
                })
        return action, {}


class GeneralLedgerLine(ModelSQL, ModelView):
    'General Ledger Line'
    __name__ = 'account.general_ledger.line'

    move = fields.Many2One('account.move', 'Move')
    date = fields.Date('Date')
    account = fields.Many2One('account.general_ledger.account', 'Account')
    party = fields.Many2One('party.party', 'Party',
        states={
            'invisible': ~Eval('party_required', False),
            },
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})
    party_required = fields.Boolean('Party Required')
    account_party = fields.Function(
        fields.Many2One(
            'account.general_ledger.account.party', "Account Party"),
        'get_account_party')
    company = fields.Many2One('company.company', 'Company')
    debit = Monetary(
        "Debit", currency='currency', digits='currency')
    credit = Monetary(
        "Credit", currency='currency', digits='currency')
    internal_balance = Monetary(
        "Internal Balance", currency='currency', digits='currency')
    balance = fields.Function(Monetary(
            "Balance", currency='currency', digits='currency'),
        'get_balance')
    origin = fields.Reference('Origin', selection='get_origin')
    description = fields.Char('Description')
    move_description = fields.Char('Move Description')
    reconciliation = fields.Many2One(
        'account.move.reconciliation', "Reconciliation")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ], "State", sort=False)
    state_string = state.translated('state')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"), 'get_currency')

    @classmethod
    def __setup__(cls):
        super(GeneralLedgerLine, cls).__setup__()
        cls.__access__.add('account')
        cls._order.insert(0, ('date', 'ASC'))

    @classmethod
    def table_query(cls):
        pool = Pool()
        Line = pool.get('account.move.line')
        Move = pool.get('account.move')
        LedgerAccount = pool.get('account.general_ledger.account')
        Account = pool.get('account.account')
        transaction = Transaction()
        database = transaction.database
        context = transaction.context
        line = Line.__table__()
        move = Move.__table__()
        account = Account.__table__()
        columns = []
        for fname, field in cls._fields.items():
            if hasattr(field, 'set'):
                continue
            field_line = getattr(Line, fname, None)
            if fname == 'internal_balance':
                if database.has_window_functions():
                    w_columns = [line.account]
                    if context.get('party_cumulate', False):
                        w_columns.append(line.party)
                    column = Sum(line.debit - line.credit,
                        window=Window(w_columns,
                            order_by=[move.date.asc, line.id])).as_(
                                'internal_balance')
                else:
                    column = (line.debit - line.credit).as_('internal_balance')
            elif fname == 'move_description':
                column = Column(move, 'description').as_(fname)
            elif fname == 'party_required':
                column = Column(account, 'party_required').as_(fname)
            elif (not field_line
                    or fname == 'state'
                    or isinstance(field_line, fields.Function)):
                column = Column(move, fname).as_(fname)
            else:
                column = Column(line, fname).as_(fname)
            columns.append(column)
        with Transaction().set_context(LedgerAccount._debit_credit_context()):
            line_query, fiscalyear_ids = Line.query_get(line)
        return line.join(move, condition=line.move == move.id
            ).join(account, condition=line.account == account.id
                ).select(*columns, where=line_query)

    def get_currency(self, name):
        return self.company.currency.id

    @classmethod
    def get_origin(cls):
        Line = Pool().get('account.move.line')
        return Line.get_origin()

    def get_balance(self, name):
        transaction = Transaction()
        context = transaction.context
        database = transaction.database
        balance = self.internal_balance
        if database.has_window_functions():
            if context.get('party_cumulate', False) and self.account_party:
                balance += self.account_party.start_balance
            else:
                balance += self.account.start_balance
        return balance

    @classmethod
    def get_account_party(cls, records, name):
        pool = Pool()
        AccountParty = pool.get('account.general_ledger.account.party')
        account_party = AccountParty.__table__()
        cursor = Transaction().connection.cursor()

        account_parties = {}
        account_party2ids = defaultdict(list)
        account_ids, party_ids = set(), set()
        for r in records:
            account_parties[r.id] = None
            account_party2ids[r.account.id, r.party.id].append(r.id)
            account_ids.add(r.account.id)
            party_ids.add(r.party.id)

        query = account_party.select(
            account_party.account, account_party.party, account_party.id)
        for sub_account_ids in grouped_slice(account_ids):
            account_where = reduce_ids(account_party.account, sub_account_ids)
            for sub_party_ids in grouped_slice(party_ids):
                query.where = (account_where
                    & reduce_ids(account_party.party, sub_party_ids))
                cursor.execute(*query)
                for account, party, id_ in cursor:
                    key = (account, party)
                    try:
                        account_party_ids = account_party2ids[key]
                    except KeyError:
                        # There can be more combinations of account-party in
                        # the database than from records
                        continue
                    for record_id in account_party_ids:
                        account_parties[record_id] = id_
        return account_parties


class GeneralLedgerLineContext(GeneralLedgerAccountContext):
    'General Ledger Line Context'
    __name__ = 'account.general_ledger.line.context'

    party_cumulate = fields.Boolean('Cumulate per Party')

    @classmethod
    def default_party_cumulate(cls):
        return False


class GeneralLedger(Report):
    __name__ = 'account.general_ledger'

    @classmethod
    def get_context(cls, records, header, data):
        pool = Pool()
        Company = pool.get('company.company')
        Fiscalyear = pool.get('account.fiscalyear')
        Period = pool.get('account.period')
        context = Transaction().context

        report_context = super().get_context(records, header, data)

        report_context['company'] = Company(context['company'])
        report_context['fiscalyear'] = Fiscalyear(context['fiscalyear'])

        for period in ['start_period', 'end_period']:
            if context.get(period):
                report_context[period] = Period(context[period])
            else:
                report_context[period] = None
        report_context['from_date'] = context.get('from_date')
        report_context['to_date'] = context.get('to_date')

        report_context['accounts'] = records
        return report_context


class TrialBalance(Report):
    __name__ = 'account.trial_balance'

    @classmethod
    def get_context(cls, records, header, data):
        pool = Pool()
        Company = pool.get('company.company')
        Fiscalyear = pool.get('account.fiscalyear')
        Period = pool.get('account.period')
        context = Transaction().context

        report_context = super().get_context(records, header, data)

        report_context['company'] = Company(context['company'])
        report_context['fiscalyear'] = Fiscalyear(context['fiscalyear'])

        for period in ['start_period', 'end_period']:
            if context.get(period):
                report_context[period] = Period(context[period])
            else:
                report_context[period] = None
        report_context['from_date'] = context.get('from_date')
        report_context['to_date'] = context.get('to_date')

        report_context['accounts'] = records
        report_context['sum'] = cls.sum
        return report_context

    @classmethod
    def sum(cls, accounts, field):
        return sum((getattr(a, field) for a in accounts), Decimal('0'))


class BalanceSheetContext(ModelView):
    'Balance Sheet Context'
    __name__ = 'account.balance_sheet.context'
    date = fields.Date('Date', required=True)
    company = fields.Many2One('company.company', 'Company', required=True)
    posted = fields.Boolean('Posted Move', help="Only include posted moves.")

    @staticmethod
    def default_date():
        Date_ = Pool().get('ir.date')
        return Transaction().context.get('date', Date_.today())

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_posted():
        return Transaction().context.get('posted', False)


class BalanceSheetComparisionContext(BalanceSheetContext):
    'Balance Sheet Context'
    __name__ = 'account.balance_sheet.comparison.context'
    comparison = fields.Boolean('Comparison')
    date_cmp = fields.Date('Date', states={
            'required': Eval('comparison', False),
            'invisible': ~Eval('comparison', False),
            })

    @classmethod
    def default_comparison(cls):
        return False

    @fields.depends('comparison', 'date', 'date_cmp')
    def on_change_comparison(self):
        self.date_cmp = None
        if self.comparison and self.date:
            self.date_cmp = self.date - relativedelta(years=1)

    @classmethod
    def view_attributes(cls):
        return super().view_attributes() + [
            ('/form/separator[@id="comparison"]', 'states', {
                    'invisible': ~Eval('comparison', False),
                    }),
            ]


class IncomeStatementContext(ModelView):
    'Income Statement Context'
    __name__ = 'account.income_statement.context'
    fiscalyear = fields.Many2One('account.fiscalyear', 'Fiscal Year',
        required=True,
        domain=[
            ('company', '=', Eval('company')),
            ])
    start_period = fields.Many2One('account.period', 'Start Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear')),
            ('start_date', '<=', (Eval('end_period'), 'start_date'))
            ],
        states={
            'invisible': Eval('from_date', False) | Eval('to_date', False),
            })
    end_period = fields.Many2One('account.period', 'End Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear')),
            ('start_date', '>=', (Eval('start_period'), 'start_date')),
            ],
        states={
            'invisible': Eval('from_date', False) | Eval('to_date', False),
            })
    from_date = fields.Date("From Date",
        domain=[
            If(Eval('to_date') & Eval('from_date'),
                ('from_date', '<=', Eval('to_date')),
                ()),
            ],
        states={
            'invisible': (
                Eval('start_period', False) | Eval('end_period', False)),
            })
    to_date = fields.Date("To Date",
        domain=[
            If(Eval('from_date') & Eval('to_date'),
                ('to_date', '>=', Eval('from_date')),
                ()),
            ],
        states={
            'invisible': (
                Eval('start_period', False) | Eval('end_period', False)),
            })
    company = fields.Many2One('company.company', 'Company', required=True)
    posted = fields.Boolean('Posted Move', help="Only include posted moves.")
    comparison = fields.Boolean('Comparison')
    fiscalyear_cmp = fields.Many2One('account.fiscalyear', 'Fiscal Year',
        states={
            'required': Eval('comparison', False),
            'invisible': ~Eval('comparison', False),
            },
        domain=[
            ('company', '=', Eval('company')),
            ])
    start_period_cmp = fields.Many2One('account.period', 'Start Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear_cmp')),
            ('start_date', '<=', (Eval('end_period_cmp'), 'start_date'))
            ],
        states={
            'invisible': ~Eval('comparison', False),
            })
    end_period_cmp = fields.Many2One('account.period', 'End Period',
        domain=[
            ('fiscalyear', '=', Eval('fiscalyear_cmp')),
            ('start_date', '>=', (Eval('start_period_cmp'), 'start_date')),
            ],
        states={
            'invisible': ~Eval('comparison', False),
            })
    from_date_cmp = fields.Date("From Date",
        domain=[
            If(Eval('to_date_cmp') & Eval('from_date_cmp'),
                ('from_date_cmp', '<=', Eval('to_date_cmp')),
                ()),
            ],
        states={
            'invisible': ~Eval('comparison', False),
            })
    to_date_cmp = fields.Date("To Date",
        domain=[
            If(Eval('from_date_cmp') & Eval('to_date_cmp'),
                ('to_date_cmp', '>=', Eval('from_date_cmp')),
                ()),
            ],
        states={
            'invisible': ~Eval('comparison', False),
            })

    @staticmethod
    def default_fiscalyear():
        FiscalYear = Pool().get('account.fiscalyear')
        return FiscalYear.find(
            Transaction().context.get('company'), exception=False)

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @staticmethod
    def default_posted():
        return False

    @classmethod
    def default_comparison(cls):
        return False

    @fields.depends('company', 'fiscalyear', methods=['on_change_fiscalyear'])
    def on_change_company(self):
        if self.fiscalyear and self.fiscalyear.company != self.company:
            self.fiscalyear = None
            self.on_change_fiscalyear()

    @fields.depends('fiscalyear', 'start_period', 'end_period')
    def on_change_fiscalyear(self):
        if (self.start_period
                and self.start_period.fiscalyear != self.fiscalyear):
            self.start_period = None
        if (self.end_period
                and self.end_period.fiscalyear != self.fiscalyear):
            self.end_period = None

    @fields.depends('start_period')
    def on_change_start_period(self):
        if self.start_period:
            self.from_date = self.to_date = None

    @fields.depends('end_period')
    def on_change_end_period(self):
        if self.end_period:
            self.from_date = self.to_date = None

    @fields.depends('from_date')
    def on_change_from_date(self):
        if self.from_date:
            self.start_period = self.end_period = None

    @fields.depends('to_date')
    def on_change_to_date(self):
        if self.to_date:
            self.start_period = self.end_period = None

    @classmethod
    def view_attributes(cls):
        return super().view_attributes() + [
            ('/form/separator[@id="comparison"]', 'states', {
                    'invisible': ~Eval('comparison', False),
                    }),
            ]


class AgedBalanceContext(ModelView):
    'Aged Balance Context'
    __name__ = 'account.aged_balance.context'
    type = fields.Selection([
            ('customer', 'Customers'),
            ('supplier', 'Suppliers'),
            ('customer_supplier', 'Customers and Suppliers'),
            ],
        "Type", required=True)
    date = fields.Date('Date', required=True)
    term1 = fields.Integer("First Term", required=True)
    term2 = fields.Integer("Second Term", required=True,
        domain=[
            ('term2', '>', Eval('term1', 0)),
            ])
    term3 = fields.Integer("Third Term", required=True,
        domain=[
            ('term3', '>', Eval('term2', 0)),
            ])
    unit = fields.Selection([
            ('day', 'Days'),
            ('week', "Weeks"),
            ('month', 'Months'),
            ('year', "Years"),
            ], "Unit", required=True, sort=False)
    company = fields.Many2One('company.company', 'Company', required=True)
    posted = fields.Boolean('Posted Move', help="Only include posted moves.")

    @classmethod
    def default_type(cls):
        return 'customer'

    @classmethod
    def default_posted(cls):
        return False

    @classmethod
    def default_date(cls):
        return Pool().get('ir.date').today()

    @classmethod
    def default_term1(cls):
        return cls._default_terms(cls.default_unit())[0]

    @classmethod
    def default_term2(cls):
        return cls._default_terms(cls.default_unit())[1]

    @classmethod
    def default_term3(cls):
        return cls._default_terms(cls.default_unit())[2]

    @classmethod
    def default_unit(cls):
        return 'day'

    @fields.depends('unit')
    def on_change_unit(self):
        self.term1, self.term2, self.term3 = self._default_terms(self.unit)

    @classmethod
    def _default_terms(cls, unit):
        terms = None, None, None
        if unit == 'day':
            terms = 30, 60, 90
        elif unit == 'week':
            terms = 4, 8, 12
        elif unit in {'month', 'year'}:
            terms = 1, 2, 3
        return terms

    @staticmethod
    def default_company():
        return Transaction().context.get('company')


class AgedBalance(ModelSQL, ModelView):
    'Aged Balance'
    __name__ = 'account.aged_balance'

    party = fields.Many2One(
        'party.party', 'Party',
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})
    company = fields.Many2One('company.company', 'Company')
    term0 = Monetary(
        "Now", currency='currency', digits='currency')
    term1 = Monetary(
        "First Term", currency='currency', digits='currency')
    term2 = Monetary(
        "Second Term", currency='currency', digits='currency')
    term3 = Monetary(
        "Third Term", currency='currency', digits='currency')
    balance = Monetary(
        "Balance", currency='currency', digits='currency')
    currency = fields.Function(fields.Many2One(
            'currency.currency', "Currency"), 'get_currency')

    @classmethod
    def __setup__(cls):
        super(AgedBalance, cls).__setup__()
        cls._order.insert(0, ('party', 'ASC'))

    @classmethod
    def table_query(cls):
        pool = Pool()
        context = Transaction().context
        MoveLine = pool.get('account.move.line')
        Move = pool.get('account.move')
        Reconciliation = pool.get('account.move.reconciliation')
        Account = pool.get('account.account')
        Type = pool.get('account.account.type')

        line = MoveLine.__table__()
        move = Move.__table__()
        reconciliation = Reconciliation.__table__()
        account = Account.__table__()
        type_ = Type.__table__()
        debit_type = Type.__table__()
        credit_type = Type.__table__()

        company_id = context.get('company')
        date = context.get('date')
        with Transaction().set_context(date=None):
            line_query, _ = MoveLine.query_get(line)
        kind = cls.get_kind(type_)
        debit_kind = cls.get_kind(debit_type)
        credit_kind = cls.get_kind(credit_type)
        columns = [
            line.party.as_('id'),
            Literal(0).as_('create_uid'),
            Max(line.create_date).as_('create_date'),
            Literal(0).as_('write_uid'),
            Max(line.write_date).as_('write_date'),
            line.party.as_('party'),
            move.company.as_('company'),
            (Sum(line.debit) - Sum(line.credit)).as_('balance'),
            ]

        terms = cls.get_terms()
        factor = cls.get_unit_factor()
        # Ensure None are before 0 to get the next index pointing to the next
        # value and not a None value
        term_values = sorted(
            list(terms.values()), key=lambda x: ((x is not None), x or 0))

        line_date = Coalesce(line.maturity_date, move.date)
        for name, value in terms.items():
            if value is None or factor is None or date is None:
                columns.append(Literal(None).as_(name))
                continue
            cond = line_date <= (date - value * factor)
            idx = term_values.index(value)
            if idx + 1 < len(terms):
                cond &= line_date > (
                    date - (term_values[idx + 1] or 0) * factor)
            columns.append(
                Sum(Case((cond, line.debit - line.credit), else_=0)).as_(name))

        return line.join(move, condition=line.move == move.id
            ).join(account, condition=line.account == account.id
            ).join(type_, condition=account.type == type_.id
            ).join(debit_type, 'LEFT',
                condition=account.debit_type == debit_type.id
            ).join(credit_type, 'LEFT',
                condition=account.credit_type == credit_type.id
            ).join(reconciliation, 'LEFT',
                condition=reconciliation.id == line.reconciliation
            ).select(*columns,
                where=(line.party != Null)
                & (kind | debit_kind | credit_kind)
                & ((line.reconciliation == Null)
                    | (reconciliation.date > date))
                & (move.date <= date)
                & (account.company == company_id)
                & line_query,
                group_by=(line.party, move.company))

    @classmethod
    def get_terms(cls):
        context = Transaction().context
        return {
            'term0': 0,
            'term1': context.get('term1'),
            'term2': context.get('term2'),
            'term3': context.get('term3'),
            }

    @classmethod
    def get_unit_factor(cls):
        context = Transaction().context
        return {
            'year': relativedelta(years=1),
            'month': relativedelta(months=1),
            'week': relativedelta(weeks=1),
            'day': relativedelta(days=1)
            }.get(context.get('unit', 'day'))

    @classmethod
    def get_kind(cls, account_type):
        context = Transaction().context
        type_ = context.get('type', 'customer')
        if type_ == 'customer_supplier':
            return account_type.payable | account_type.receivable
        elif type_ == 'supplier':
            return account_type.payable
        elif type_ == 'customer':
            return account_type.receivable
        else:
            return Literal(False)

    def get_currency(self, name):
        return self.company.currency.id


class AgedBalanceReport(Report):
    __name__ = 'account.aged_balance'

    @classmethod
    def get_context(cls, records, header, data):
        pool = Pool()
        Company = pool.get('company.company')
        Context = pool.get('account.aged_balance.context')
        AgedBalance = pool.get('account.aged_balance')
        context = Transaction().context

        report_context = super().get_context(records, header, data)

        context_fields = Context.fields_get(['type', 'unit'])

        report_context['company'] = Company(context['company'])
        report_context['date'] = context['date']
        report_context['type'] = dict(
            context_fields['type']['selection'])[context['type']]
        report_context['unit'] = dict(
            context_fields['unit']['selection'])[context['unit']]
        report_context.update(AgedBalance.get_terms())
        report_context['sum'] = cls.sum
        return report_context

    @classmethod
    def sum(cls, records, field):
        return sum((getattr(r, field) for r in records), Decimal('0'))


class CreateChartStart(ModelView):
    'Create Chart'
    __name__ = 'account.create_chart.start'


class CreateChartAccount(ModelView):
    'Create Chart'
    __name__ = 'account.create_chart.account'
    company = fields.Many2One('company.company', 'Company', required=True)
    account_template = fields.Many2One('account.account.template',
            'Account Template', required=True, domain=[('parent', '=', None)])

    @staticmethod
    def default_company():
        return Transaction().context.get('company')


class CreateChartProperties(ModelView):
    'Create Chart'
    __name__ = 'account.create_chart.properties'
    company = fields.Many2One('company.company', 'Company')
    account_receivable = fields.Many2One('account.account',
            'Default Receivable Account',
            domain=[
                ('closed', '!=', True),
                ('type.receivable', '=', True),
                ('party_required', '=', True),
                ('company', '=', Eval('company')),
                ])
    account_payable = fields.Many2One('account.account',
            'Default Payable Account',
            domain=[
                ('closed', '!=', True),
                ('type.payable', '=', True),
                ('party_required', '=', True),
                ('company', '=', Eval('company')),
                ])


class CreateChart(Wizard):
    'Create Chart'
    __name__ = 'account.create_chart'
    start = StateView('account.create_chart.start',
        'account.create_chart_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('OK', 'account', 'tryton-ok', default=True),
            ])
    account = StateView('account.create_chart.account',
        'account.create_chart_account_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Create', 'create_account', 'tryton-ok', default=True),
            ])
    create_account = StateTransition()
    properties = StateView('account.create_chart.properties',
        'account.create_chart_properties_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Create', 'create_properties', 'tryton-ok', default=True),
            ])
    create_properties = StateTransition()

    def transition_create_account(self):
        pool = Pool()
        TaxCodeTemplate = pool.get('account.tax.code.template')
        TaxCodeLineTemplate = pool.get('account.tax.code.line.template')
        TaxTemplate = pool.get('account.tax.template')
        TaxRuleTemplate = pool.get('account.tax.rule.template')
        TaxRuleLineTemplate = \
            pool.get('account.tax.rule.line.template')
        Config = pool.get('ir.configuration')
        Account = pool.get('account.account')
        Warning = pool.get('res.user.warning')
        transaction = Transaction()

        company = self.account.company
        # Skip access rule
        with transaction.set_user(0):
            accounts = Account.search([('company', '=', company.id)], limit=1)
        if accounts:
            key = 'duplicated_chart.%d' % company.id
            if Warning.check(key):
                raise ChartWarning(key,
                    gettext('account.msg_account_chart_exists',
                        company=company.rec_name))

        with transaction.set_context(language=Config.get_language(),
                company=company.id):
            account_template = self.account.account_template

            # Create account types
            template2type = {}
            account_template.type.create_type(
                company.id,
                template2type=template2type)

            # Create accounts
            template2account = {}
            account_template.create_account(
                company.id,
                template2account=template2account,
                template2type=template2type)

            # Create taxes
            template2tax = {}
            TaxTemplate.create_tax(
                account_template.id, company.id,
                template2account=template2account,
                template2tax=template2tax)

            # Create tax codes
            template2tax_code = {}
            TaxCodeTemplate.create_tax_code(
                account_template.id, company.id,
                template2tax_code=template2tax_code)

            # Create tax code lines
            template2tax_code_line = {}
            TaxCodeLineTemplate.create_tax_code_line(
                account_template.id,
                template2tax=template2tax,
                template2tax_code=template2tax_code,
                template2tax_code_line=template2tax_code_line)

            # Update taxes and replaced_by on accounts
            account_template.update_account2(template2account, template2tax)

            # Create tax rules
            template2rule = {}
            TaxRuleTemplate.create_rule(
                account_template.id, company.id,
                template2rule=template2rule)

            # Create tax rule lines
            template2rule_line = {}
            TaxRuleLineTemplate.create_rule_line(
                account_template.id, template2tax, template2rule,
                template2rule_line=template2rule_line)
        return 'properties'

    def get_account(self, template_id):
        pool = Pool()
        Account = pool.get('account.account')
        ModelData = pool.get('ir.model.data')
        template_id = ModelData.get_id(template_id)
        account, = Account.search([
                ('template', '=', template_id),
                ('company', '=', self.account.company.id),
                ], limit=1)
        return account.id

    def default_properties(self, fields):
        pool = Pool()
        Account = pool.get('account.account')

        defaults = {
            'company': self.account.company.id,
            }

        receivable_accounts = Account.search([
                ('type.receivable', '=', True),
                ('company', '=', self.account.company.id),
                ], limit=2)
        payable_accounts = Account.search([
                ('type.payable', '=', True),
                ('company', '=', self.account.company.id),
                ], limit=2)

        if len(receivable_accounts) == 1:
            defaults['account_receivable'] = receivable_accounts[0].id
        else:
            defaults['account_receivable'] = None
        if len(payable_accounts) == 1:
            defaults['account_payable'] = payable_accounts[0].id
        else:
            defaults['account_payable'] = None

        return defaults

    def transition_create_properties(self):
        pool = Pool()
        Configuration = pool.get('account.configuration')

        with Transaction().set_context(company=self.properties.company.id):
            account_receivable = self.properties.account_receivable
            account_payable = self.properties.account_payable
            config = Configuration(1)
            config.default_account_receivable = account_receivable
            config.default_account_payable = account_payable
            config.save()
        return 'end'


class UpdateChartStart(ModelView):
    'Update Chart'
    __name__ = 'account.update_chart.start'
    account = fields.Many2One(
        'account.account', "Root Account", required=True,
        domain=[
            ('parent', '=', None),
            ('template', '!=', None),
            ])


class UpdateChartSucceed(ModelView):
    'Update Chart'
    __name__ = 'account.update_chart.succeed'


class UpdateChart(Wizard):
    'Update Chart'
    __name__ = 'account.update_chart'
    start = StateView('account.update_chart.start',
        'account.update_chart_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Update', 'update', 'tryton-ok', default=True),
            ])
    update = StateTransition()
    succeed = StateView('account.update_chart.succeed',
        'account.update_chart_succeed_view_form', [
            Button('OK', 'end', 'tryton-ok', default=True),
            ])

    def default_start(self, fields):
        pool = Pool()
        Account = pool.get('account.account')

        defaults = {}
        with Transaction().set_context(_check_access=True):
            charts = Account.search([
                    ('parent', '=', None),
                    ('template', '!=', None),
                    ], limit=2)
        if len(charts) == 1:
            defaults['account'] = charts[0].id
        return defaults

    @inactive_records
    def transition_update(self):
        pool = Pool()
        Account = pool.get('account.account')
        TaxCode = pool.get('account.tax.code')
        TaxCodeTemplate = pool.get('account.tax.code.template')
        TaxCodeLine = pool.get('account.tax.code.line')
        TaxCodeLineTemplate = pool.get('account.tax.code.line.template')
        Tax = pool.get('account.tax')
        TaxTemplate = pool.get('account.tax.template')
        TaxRule = pool.get('account.tax.rule')
        TaxRuleTemplate = pool.get('account.tax.rule.template')
        TaxRuleLine = pool.get('account.tax.rule.line')
        TaxRuleLineTemplate = \
            pool.get('account.tax.rule.line.template')

        # re-browse to have inactive context
        account = Account(self.start.account.id)
        company = account.company

        # Update account types
        template2type = {}
        account.type.update_type(template2type=template2type)
        # Create missing account types
        if account.type.template:
            account.type.template.create_type(
                company.id,
                template2type=template2type)

        # Update accounts
        template2account = {}
        account.update_account(template2account=template2account,
            template2type=template2type)
        # Create missing accounts
        if account.template:
            account.template.create_account(
                company.id,
                template2account=template2account,
                template2type=template2type)

        # Update taxes
        template2tax = {}
        Tax.update_tax(
            company.id,
            template2account=template2account,
            template2tax=template2tax)
        # Create missing taxes
        if account.template:
            TaxTemplate.create_tax(
                account.template.id, account.company.id,
                template2account=template2account,
                template2tax=template2tax)

        # Update tax codes
        template2tax_code = {}
        TaxCode.update_tax_code(
            company.id,
            template2tax_code=template2tax_code)
        # Create missing tax codes
        if account.template:
            TaxCodeTemplate.create_tax_code(
                account.template.id, company.id,
                template2tax_code=template2tax_code)

        # Update tax code lines
        template2tax_code_line = {}
        TaxCodeLine.update_tax_code_line(
            company.id,
            template2tax=template2tax,
            template2tax_code=template2tax_code,
            template2tax_code_line=template2tax_code_line)
        # Create missing tax code lines
        if account.template:
            TaxCodeLineTemplate.create_tax_code_line(
                account.template.id,
                template2tax=template2tax,
                template2tax_code=template2tax_code,
                template2tax_code_line=template2tax_code_line)

        # Update taxes and replaced_by on accounts
        account.update_account2(template2account, template2tax)

        # Update tax rules
        template2rule = {}
        TaxRule.update_rule(company.id, template2rule=template2rule)
        # Create missing tax rules
        if account.template:
            TaxRuleTemplate.create_rule(
                account.template.id, account.company.id,
                template2rule=template2rule)

        # Update tax rule lines
        template2rule_line = {}
        TaxRuleLine.update_rule_line(
            company.id, template2tax, template2rule,
            template2rule_line=template2rule_line)
        # Create missing tax rule lines
        if account.template:
            TaxRuleLineTemplate.create_rule_line(
                account.template.id, template2tax, template2rule,
                template2rule_line=template2rule_line)
        return 'succeed'
