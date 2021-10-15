"""PortfolioSummary extension for Fava
Report out summary information for groups of portfolios
Similar extensions:
https://github.com/scauligi/refried
https://github.com/seltzered/fava-classy-portfolio
https://github.com/redstreet/fava_investor
https://github.com/redstreet/fava_tax_loss_harvester

IRR calculation copied from:
https://github.com/hoostus/portfolio-returns

This is a simple example of Fava's extension reports system.
"""
import re
import time
from collections.abc import Iterable

from beancount.core.number import Decimal
from beancount.core.number import ZERO

from fava.ext import FavaExtensionBase
from fava.helpers import FavaAPIException
from fava.core.conversion import cost_or_value
from fava.context import g
from portfolio_summary.irr import IRR


class PortfolioSummary(FavaExtensionBase):  # pragma: no cover
    """Report out summary information for groups of portfolios"""

    report_title = "Portfolio List"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.all_mwr_accounts = None
        self.irr = None
        self.total = None
        self.operating_currency = None

    def portfolio_accounts(self):
        """An account tree based on matching regex patterns."""
        self.operating_currency = self.ledger.options["operating_currency"][0]
        self.total = {
            'account': 'Total',
            'balance': ZERO,
            'cost': ZERO,
            'allocation': 100,
            'children': []
            }
        self.all_mwr_accounts = set()
        all_mwr_internal = set()
        tree = self.ledger.root_tree
        any_mwr = False
        any_twr = False
        portfolios = []
        _t0 = time.time()
        self.irr = IRR(self.ledger.all_entries, g.ledger.price_map, self.operating_currency)
        for key, pattern, internal, mwr, twr in self.parse_config():
            any_mwr |= bool(mwr)
            any_twr |= bool(twr)
            try:
                portfolio = self._account_metadata_pattern(tree, key, pattern, internal, mwr, twr)
            except Exception as _e:
                # We re-raise to prevent masking the error.  Should this be a FavaAPIException?
                raise Exception from _e
            all_mwr_internal |= internal
            portfolios.append(portfolio)
        self.total['change'] = round((float(self.total['balance'] - self.total['cost']) /
                                     (float(self.total['cost'])+.00001)) * 100, 2)
        if any_mwr or any_twr:
            self.total['mwr'], self.total['twr'] = self._calculate_irr_twr(
                self.all_mwr_accounts, all_mwr_internal, any_mwr, any_twr)
        portfolios = [("All portfolios", (self._get_types(any_mwr, any_twr), [self.total]))] + portfolios
        print(f"Done: Elaped: {time.time() - _t0:.2f} (mwr/twr: {self.irr.elapsed():.2f})")
        return portfolios

    def parse_config(self):
        """Parse configuration options"""
        # pylint: disable=unsubscriptable-object not-an-iterable unsupported-membership-test
        keys = ('metadata-key', 'account-groups', 'internal', 'mwr', 'twr')
        if not isinstance(self.config, dict):
            raise FavaAPIException("Portfolio List: Config should be a dictionary.")
        for key in ('metadata-key', 'account-groups'):
            if key not in self.config:
                raise FavaAPIException(f"Portfolio List: '{key}' is required key.")
        for key in self.config:
            if key not in keys:
                raise FavaAPIException(f"Portfolio List: '{key}' is an invalid key.")
        internal = self.config.get('internal', set())
        if isinstance(internal, (tuple, list)):
            internal = set(internal)
        elif not isinstance(internal, set):
            raise FavaAPIException("Portfolio List: 'internal' must be a list.")
        mwr = self.config.get('mwr', True)
        twr = self.config.get('twr', False)  # TWR is expensive to calculate
        if isinstance(mwr, str) and mwr != "children":
            raise FavaAPIException("Portfolio List: 'mwr' must be one of (True, False, 'children')")
        if isinstance(twr, str) and twr != "children":
            raise FavaAPIException("Portfolio List: 'twr' must be one of (True, False, 'children')")
        for group in self.config['account-groups']:
            grp_internal = internal.copy()
            grp_mwr = mwr
            grp_twr = twr
            if isinstance(group, dict):
                try:
                    grp_internal |= set(group.get('internal', set()))
                    grp_mwr = group.get('mwr', mwr)
                    grp_twr = group.get('twr', twr)
                    group = group['name']
                except Exception as _e:
                    raise FavaAPIException(f"Portfolio List: Error parsing group {str(group)}: {str(_e)}") from _e
            yield self.config['metadata-key'], group, grp_internal, grp_mwr, grp_twr

    @staticmethod
    def _get_types(mwr, twr):
        types = []
        types.append(("account", str(str)))
        types.append(("balance", str(Decimal)))
        types.append(("cost", str(Decimal)))
        if mwr:
            types.append(("mwr", 'Percent'))
        if twr:
            types.append(("twr", 'Percent'))
        types.append(("change", 'Percent'))
        types.append(("allocation", 'Percent'))
        return types

    def _account_metadata_pattern(self, tree, metadata_key, pattern, internal, mwr, twr):
        """
        Returns portfolio info based on matching account open metadata.

        Args:
            tree: Ledger root tree node.
            metadata_key: Metadata key to match for in account open.
            pattern: Metadata value's regex pattern to match for.
        Return:
            Data structured for use with a querytable - (types, rows).
        """
        # pylint: disable=too-many-arguments
        title = f"{pattern.capitalize()} portfolios"
        selected_accounts = []
        regexer = re.compile(pattern)
        accounts = self.ledger.all_entries_by_type.Open
        accounts = sorted(accounts, key=lambda x: x.account)
        last_seen = None
        for entry in accounts:
            if entry.account not in tree:
                continue
            if (metadata_key in entry.meta) and (
                regexer.match(entry.meta[metadata_key]) is not None
            ):
                selected_accounts.append({'account': tree[entry.account], 'children': []})
                last_seen = entry.account + ':'
            elif last_seen and entry.account.startswith(last_seen):
                selected_accounts[-1]['children'].append(tree[entry.account])

        portfolio_data = self._portfolio_data(selected_accounts, internal, mwr, twr)
        return title, portfolio_data

    def _process_node(self, node):
        row = {}
        row["account"] = node.name
        row['children'] = []
        date=self.ledger.end_date
        balance = cost_or_value(node.balance, "at_value", g.ledger.price_map, date=date)
        cost = cost_or_value(node.balance, "at_cost", g.ledger.price_map, date=date)
        if self.operating_currency in balance and self.operating_currency in cost:
            balance_dec = round(balance[self.operating_currency], 2)
            cost_dec = round(cost[self.operating_currency], 2)
            row["balance"] = balance_dec
            row["cost"] = cost_dec
        return row

    def _portfolio_data(self, nodes, internal, mwr, twr):
        """
        Turn a portfolio of tree nodes into querytable-style data.

        Args:
            nodes: Account tree nodes.
        Return:
            types: Tuples of column names and types as strings.
            rows: Dictionaries of row data by column names.
        """

        rows = []
        mwr_accounts = set()
        total = {
            'account': 'Total',
            'balance': ZERO,
            'cost': ZERO,
            'children': []
            }
        rows.append(total)

        for node in nodes:
            parent = self._process_node(node['account'])
            if 'balance' not in parent:
                parent['balance'] = ZERO
                parent['cost'] = ZERO
            total['children'].append(parent)
            rows.append(parent)
            for child in node['children']:
                row = self._process_node(child)
                if 'balance' not in row:
                    continue
                parent['balance'] += row['balance']
                parent['cost'] += row['cost']
                if mwr == "children" or twr == "children":
                    row['mwr'], row['twr'] = self._calculate_irr_twr(
                        [row['account']], internal, mwr == "children", twr == "children")
                parent['children'].append(row)
                rows.append(row)
            total['balance'] += parent['balance']
            total['cost'] += parent['cost']
            if mwr or twr:
                pattern = parent['account'] + '(:.*)?'
                mwr_accounts.add(pattern)
                parent['mwr'], parent['twr'] = self._calculate_irr_twr([pattern], internal, mwr, twr)

        for row in rows:
            if "balance" in row and total['balance'] > 0:
                row["allocation"] = round((row["balance"] / total['balance']) * 100, 2)
                row["change"] = round((float(row['balance'] - row['cost']) / (float(row['cost'])+.00001)) * 100, 2)
        self.total['balance'] += total['balance']
        self.total['cost'] += total['cost']
        if mwr or twr:
            total['mwr'], total['twr'] = self._calculate_irr_twr(mwr_accounts, internal, mwr, twr)
            self.all_mwr_accounts |= mwr_accounts
        return self._get_types(mwr, twr), [total]

    def _calculate_irr_twr(self, patterns, internal, calc_mwr, calc_twr):
        mwr, twr = self.irr.calculate(
            patterns, internal_patterns=internal,
            start_date=None, end_date=self.ledger.end_date, mwr=calc_mwr, twr=calc_twr)
        if mwr:
            mwr = round(100 * mwr, 2)
        if twr:
            twr = round(100 * twr, 2)
        return mwr, twr
