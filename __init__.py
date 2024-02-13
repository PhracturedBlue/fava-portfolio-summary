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
from datetime import datetime, timedelta
import json

import re
import time
from collections.abc import Iterable
from xmlrpc.client import DateTime

from beancount.core.number import Decimal
from beancount.core.number import ZERO
from beancount.core.data import Transaction

from fava.ext import FavaExtensionBase
from fava.helpers import FavaAPIError
from fava.core.conversion import cost_or_value
from fava.core.query_shell import QueryShell
from fava.context import g
from .irr import IRR


class PortfolioSummary(FavaExtensionBase):  # pragma: no cover
    """Report out summary information for groups of portfolios"""

    report_title = "Portfolio Summary"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accounts = None
        self.irr_cache = {}
        self.dividend_cache = {}

    def portfolio_accounts(self):
        """An account tree based on matching regex patterns."""
        if self.ledger.accounts is not self.accounts:
            # self.ledger.accounts should be reset every time the databse is loaded
            self.dividend_cache = {}
            self.irr_cache = {}
            self.accounts = self.ledger.accounts
        portfolio_summary = PortfolioSummaryInstance(self.ledger, self.config, self.irr_cache, self.dividend_cache)
        return portfolio_summary.run()

class PortfolioSummaryInstance:  # pragma: no cover
    """Thread-safe instance of Portfolio Summary"""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, ledger, config, irr_cache, dividend_cache):
        self.ledger = ledger
        self.config = config
        self.irr_cache = irr_cache
        self.dividend_cache = dividend_cache
        self.operating_currency = self.ledger.options["operating_currency"][0]
        self.irr = IRR(self.ledger.all_entries, g.ledger.price_map, self.operating_currency, errors=self.ledger.errors)
        self.all_mwr_accounts = set()
        self.dividends_elapsed = 0
        self.total = {
            'account': 'Total',
            'balance': ZERO,
            'cost': ZERO,
            'pnl':ZERO,
            'dividends':ZERO,
            'allocation': 100,
            'mwr': ZERO,
            'twr': ZERO,
            'children': [],
            'last-date':None
            }
        self.all_cols = ["units", "cost", "balance", "pnl", "dividends", "change", "mwr", "twr", "allocation"]

    def run(self):
        """Calculdate summary"""
        all_mwr_internal = set()
        tree = g.filtered.root_tree
        portfolios = []
        _t0 = time.time()
        seen_cols = {}  # Use a dict instead of a set to preserve order
        for res in self.parse_config():
            if len(res) == 1:
                cols = [_ for _ in res[0] if _ in seen_cols]
                break
            key, pattern, internal, cols, mwr, twr = res
            seen_cols.update({_: None for _ in cols})
            try:
                title, portfolio = self._account_metadata_pattern(
                    tree, key, pattern, internal, mwr, twr, "dividends" in cols)
            except Exception as _e:
                # We re-raise to prevent masking the error.  Should this be a FavaAPIError?
                raise Exception from _e
            all_mwr_internal |= internal
            portfolios.append((title, (self._get_types(cols), [portfolio])))

        #Adds allocation for each portfolio under All portfolios
        portfolio_summary = []
        for title, portfolio_data in portfolios:
            for row in portfolio_data[1]:
                if row['account'] == 'Total':
                    row_copy = row.copy()
                    row_copy['account'] = title
                    row_copy['allocation'] = round(100*(float(row['balance'])/float(self.total['balance'])),2)
                    row_copy['children'] = []
                    portfolio_summary.append(row_copy)
        self.total['children'] = portfolio_summary

        self.total['change'] = round((float(self.total['balance'] - self.total['cost']) /
                                     (float(self.total['cost'])+.00001)) * 100, 2)
        self.total['pnl'] = round(float(self.total['balance'] - self.total['cost']), 2)
        if 'mwr' in seen_cols or 'twr' in seen_cols:
            self.total['mwr'], self.total['twr'] = self._calculate_irr_twr(
                self.all_mwr_accounts, all_mwr_internal, 'mwr' in seen_cols, 'twr' in seen_cols)

        portfolios = [("All portfolios", (self._get_types(cols), [self.total]))] + portfolios
        print(f"Done: Elapsed: {time.time() - _t0:.2f} (mwr/twr: {self.irr.elapsed():.2f}, "
              f"dividends: {self.dividends_elapsed: .2f})")
        return portfolios

    def parse_config(self):
        """Parse configuration options"""
        # pylint: disable=unsubscriptable-object not-an-iterable unsupported-membership-test
        keys = ('metadata-key', 'account-groups', 'internal', 'mwr', 'twr', 'dividends', 'cols')
        if not isinstance(self.config, dict):
            raise FavaAPIError("Portfolio List: Config should be a dictionary.")
        for key in ('metadata-key', 'account-groups'):
            if key not in self.config:
                raise FavaAPIError(f"Portfolio List: '{key}' is required key.")
        for key in self.config:
            if key not in keys:
                raise FavaAPIError(f"Portfolio List: '{key}' is an invalid key.")
        internal = self.config.get('internal', set())
        if isinstance(internal, (tuple, list)):
            internal = set(internal)
        elif not isinstance(internal, set):
            raise FavaAPIError("Portfolio List: 'internal' must be a list.")
        cols = self.config.get('cols', self.all_cols.copy())
        for col in cols:
            if col not in self.all_cols:
                raise FavaAPIError(f"Portfolio List: '{col}' is not a valid column. "
                                       f"Must be one of {self.all_cols}")
        mwr = self.config.get('mwr', 'mwr' in cols)
        # twr and dividends are expensive to calculate, so default to disabled
        twr = self.config.get('twr', 'twr' in self.config.get('cols', []))
        dividends = self.config.get('dividends', 'dividends' in self.config.get('cols', []))
        if isinstance(mwr, str) and mwr != "children":
            raise FavaAPIError("Portfolio List: 'mwr' must be one of (True, False, 'children')")
        if isinstance(twr, str) and twr != "children":
            raise FavaAPIError("Portfolio List: 'twr' must be one of (True, False, 'children')")
        if isinstance(dividends, str):
            raise FavaAPIError("Portfolio List: 'dividends' must be one of (True, False)")

        for group in self.config['account-groups']:
            yield self.config['metadata-key'], *self._parse_group(group, internal, cols, mwr, twr, dividends)

        yield [cols]

    def _parse_group(self, group, internal, cols, mwr, twr, dividends):
        grp_internal = internal.copy()
        if isinstance(group, dict):
            try:
                grp_internal |= set(group.get('internal', set()))
                grp_cols = group.get('cols', cols.copy())
                grp_mwr = group.get('mwr', mwr if 'mwr' in grp_cols else False)
                grp_twr = group.get('twr', twr if 'twr' in grp_cols else False)
                grp_dividends = group.get('dividends', dividends if 'dividends' in cols else False)
                for col in cols:
                    if col not in self.all_cols:
                        raise FavaAPIError(f"Portfolio List: '{col}' is not a valid column. "
                                               f"Must be one of {self.all_cols}")
                group = group['name']
            except Exception as _e:
                raise FavaAPIError(f"Portfolio List: Error parsing group {str(group)}: {str(_e)}") from _e
        else:
            grp_mwr = mwr
            grp_twr = twr
            grp_dividends = dividends
            grp_cols = cols.copy()
        if not grp_mwr and 'mwr' in grp_cols:
            grp_cols.remove("mwr")
        if not grp_twr and 'twr' in grp_cols:
            grp_cols.remove("twr")
        if not grp_dividends and 'dividends' in grp_cols:
            grp_cols.remove("dividends")
        return group, grp_internal, grp_cols, grp_mwr, grp_twr

    @staticmethod
    def _get_types(cols):
        col_map = {
            "units": str(Decimal),
            "cost":  str(Decimal),
            "balance":  str(Decimal),
            "pnl": str(Decimal),
            "dividends": str(Decimal),
            "change":  'Percent',
            "mwr":  'Percent',
            "twr":  'Percent',
            "allocation":  'Percent',
        }
        types = []
        types.append(("account", str(str)))
        for col in cols:
            types.append((col, col_map[col]))
        return types

    def _account_metadata_pattern(self, tree, metadata_key, pattern, internal, mwr, twr, dividends):
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
        title = f"{pattern.upper()} portfolios"
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

        portfolio_data = self._portfolio_data(selected_accounts, internal, mwr, twr, dividends)
        return title, portfolio_data


    def _process_dividends(self,account,currency):
        parent_name = ":".join(account.name.split(":")[:-1])
        cache_key = (account.name, currency,g.filtered.end_date)
        if cache_key in self.dividend_cache:
            return self.dividend_cache[cache_key]
        query = (
            f"SELECT SUM(CONVERT(COST(position),'{self.operating_currency}')) AS dividends "
            f"FROM HAS_ACCOUNT('{currency}') AND HAS_ACCOUNT('{parent_name}') WHERE LEAF(account) = 'Dividends'")
        if g.filtered.end_date:
            query += f" AND date < {g.filtered.end_date}"
        start = time.time()
        result = self.ledger.query_shell.execute_query(query)
        self.dividends_elapsed += time.time() - start
        dividends = ZERO
        if len(result[2])>0:
            for row_cost in result[2]:
                if len(row_cost.dividends.get_positions())==1:
                    dividends+=round(abs(row_cost.dividends.get_positions()[0].units.number),2)
        self.dividend_cache[cache_key] = dividends
        return dividends

    def _process_node(self, node, dividends):
        # pylint: disable=too-many-locals
        row = {}

        row["account"] = node.name
        row['children'] = []
        row["last-date"] = None
        row['pnl'] = ZERO
        row['dividends'] = ZERO
        date = g.filtered.end_date
        balance = cost_or_value(node.balance, "at_value", g.ledger.price_map, date=date)
        cost = cost_or_value(node.balance, "at_cost", g.ledger.price_map, date=date)
        #### ADD Units to the report
        units = cost_or_value(node.balance, "units", g.ledger.price_map, date=date)
        ### Get row currency
        row_currency = None
        if len(list(units.values())) > 0:
            row["units"] = list(units.values())[0]
            row_currency = list(units.keys())[0]
        #### END of UNITS
        if dividends:
            if row_currency is not None and row_currency not in self.ledger.options["operating_currency"]:
                row['dividends'] = self._process_dividends(node,row_currency)

        if self.operating_currency in balance and self.operating_currency in cost:
            balance_dec = round(balance[self.operating_currency], 2)
            cost_dec = round(cost[self.operating_currency], 2)
            row["balance"] = balance_dec
            row["cost"] = cost_dec

        #### ADD other Currencies
        elif (row_currency is not None and self.operating_currency not in balance
                and self.operating_currency not in cost):
            total_currency_cost = ZERO
            total_currency_value = ZERO

            result = self.ledger.query_shell.execute_query(
                "SELECT "
                f"convert(cost(position),'{self.operating_currency}',cost_date) AS cost, "
                f"convert(value(position) ,'{self.operating_currency}',today()) AS value "
                f"WHERE currency = '{row_currency}' AND account ='{node.name}' "
                "ORDER BY currency, cost_date")
            if len(result) == 3:
                for row_cost,row_value in result[2]:
                    total_currency_cost+=row_cost.number
                    total_currency_value+=row_value.number
            row["balance"] = round(total_currency_value, 2)
            row["cost"] = round(total_currency_cost, 2)

        ### GET LAST CURRENCY PRICE DATE
        if row_currency is not None and row_currency != self.operating_currency:
            try:
                dict_dates = g.filtered.prices(self.operating_currency,row_currency)
                if len(dict_dates) >0:
                    row["last-date"] = dict_dates[-1][0]
            except KeyError:
                pass
        return row

    def _portfolio_data(self, nodes, internal, mwr, twr, dividends):
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
            'pnl':ZERO,
            'dividends':ZERO,
            'children': [],
            'last-date':None
            }
        rows.append(total)

        for node in nodes:
            parent = self._process_node(node['account'], dividends)
            if 'balance' not in parent:
                parent['balance'] = ZERO
                parent['cost'] = ZERO
            total['children'].append(parent)
            rows.append(parent)
            for child in node['children']:
                row = self._process_node(child, dividends)
                if 'balance' not in row:
                    continue
                parent['balance'] += row['balance']
                parent['cost'] += row['cost']
                parent['dividends'] += row['dividends']
                if mwr == "children" or twr == "children":
                    row['mwr'], row['twr'] = self._calculate_irr_twr(
                        [row['account']], internal, mwr == "children", twr == "children")
                parent['children'].append(row)
                rows.append(row)
            total['balance'] += parent['balance']
            total['cost'] += parent['cost']
            total['dividends'] += parent['dividends']
            if mwr or twr:
                pattern = parent['account'] + '(:.*)?'
                mwr_accounts.add(pattern)
                parent['mwr'], parent['twr'] = self._calculate_irr_twr([pattern], internal, mwr, twr)

        for row in rows:
            if "balance" in row and total['balance'] > 0:
                row["allocation"] = round((row["balance"] / total['balance']) * 100, 2)
                row["change"] = round((float(row['balance'] - row['cost']) / (float(row['cost'])+.00001)) * 100, 2)
                row["pnl"] = round(float(row['balance'] - row['cost']),2)
        self.total['balance'] += total['balance']
        self.total['cost'] += total['cost']
        self.total['dividends'] += total['dividends']
        if mwr or twr:
            total['mwr'], total['twr'] = self._calculate_irr_twr(mwr_accounts, internal, mwr, twr)
            self.all_mwr_accounts |= mwr_accounts
        return total

    def _calculate_irr_twr(self, patterns, internal, calc_mwr, calc_twr):
        cache_key = (",".join(patterns), ",".join(internal), g.filtered.end_date, calc_mwr, calc_twr)
        if cache_key in self.irr_cache:
            return self.irr_cache[cache_key]
        mwr, twr = self.irr.calculate(
            patterns, internal_patterns=internal,
            start_date=None, end_date=g.filtered.end_date, mwr=calc_mwr, twr=calc_twr)
        if mwr:
            mwr = round(100 * mwr, 2)
        if twr:
            twr = round(100 * twr, 2)
        self.irr_cache[cache_key] = [mwr, twr]
        print(f'mwr: {mwr} twr: {twr}')
        return mwr, twr
