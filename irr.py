#!/usr/bin/env python
"""
Calculate the MWRR and/or TWRR for a list of portfolios

This code is originally from https://github.com/hoostus/portfolio-returns
Copyright Justus Pendleton
It was originally licensed under the Parity Public License 7.0.
This file is dual licensed under the Parity Public License 7.0 and the MIT License
as permitted by the Parity Public License
"""
# pylint: disable=logging-fstring-interpolation broad-except
import argparse
import logging
import sys
import functools
import operator
import collections
import datetime
import re
import time
from pprint import pprint

from decimal import Decimal
from dateutil.relativedelta import relativedelta
import beancount.loader
import beancount.utils
import beancount.core
import beancount.core.getters
import beancount.core.data
import beancount.core.convert
import beancount.parser
from fava.helpers import BeancountError

# https://github.com/peliot/XIRR-and-XNPV/blob/master/financial.py
try:
    from scipy.optimize import newton as secant_method
except:
    def secant_method(f, x0, tol=0.0001):
        """
        Solve for x where f(x)=0, given starting x0 and tolerance.
        """
        x1 = x0*1.1
        while (abs(x1-x0)/abs(x1) > tol):
            x0, x1 = x1, x1-f(x1)*(x1-x0)/(f(x1)-f(x0))
        return x1

def xnpv(rate,cashflows):
    """
    Calculate the net present value of a series of cashflows at irregular intervals.
    Arguments
    ---------
    * rate: the discount rate to be applied to the cash flows
    * cashflows: a list object in which each element is a tuple of the form (date, amount), where date is a
                 python datetime.date object and amount is an integer or floating point number. Cash outflows
                 (investments) are represented with negative amounts, and cash inflows (returns) are positive amounts.

    Returns
    -------
    * returns a single value which is the NPV of the given cash flows.
    Notes
    ---------------
    * The Net Present Value is the sum of each of cash flows discounted back to the date of the first cash flow. The
      discounted value of a given cash flow is A/(1+r)**(t-t0), where A is the amount, r is the discout rate, and
      (t-t0) is the time in years from the date of the first cash flow in the series (t0) to the date of the cash flow
      being added to the sum (t).
    * This function is equivalent to the Microsoft Excel function of the same name.
    """
    # pylint: disable=invalid-name
    chron_order = sorted(cashflows, key = lambda x: x[0])
    t0 = chron_order[0][0] #t0 is the date of the first cash flow

    return sum([cf/(1+rate)**((t-t0).days/365.0) for (t,cf) in chron_order])

def xirr(cashflows,guess=0.1):
    """
    Calculate the Internal Rate of Return of a series of cashflows at irregular intervals.
    Arguments
    ---------
    * cashflows: a list object in which each element is a tuple of the form (date, amount), where date is a
                 python datetime.date object and amount is an integer or floating point number. Cash outflows
                 (investments) are represented with negative amounts, and cash inflows (returns) are positive amounts.
    * guess (optional, default = 0.1): a guess to be used as a starting point for the numerical solution.
    Returns
    --------
    * Returns the IRR as a single value

    Notes
    ----------------
    * The Internal Rate of Return (IRR) is the discount rate at which the Net Present Value (NPV) of a series of cash
      flows is equal to zero. The NPV of the series of cash flows is determined using the xnpv function in this module.
      The discount rate at which NPV equals zero is found using the secant method of numerical solution.
    * This function is equivalent to the Microsoft Excel function of the same name.
    * For users that do not have the scipy module installed, there is an alternate version (commented out) that uses
      the secant_method function defined in the module rather than the scipy.optimize module's numerical solver. Both
      use the same method of calculation so there should be no difference in performance, but the secant_method
      function does not fail gracefully in cases where there is no solution, so the scipy.optimize.newton version is
      preferred.
    """
    try:
        return secant_method(lambda r: xnpv(r,cashflows),guess)
    except Exception as _e:
        logging.error("No solution found for IRR: %s", _e)
        return 0.0

def xtwrr(periods, debug=False):
    """Calculate TWRR from a set of date-ordered periods"""
    dates = sorted(periods.keys())
    last = float(periods[dates[0]][0])
    mean = 1.0
    if debug:
        print("Date          start-balance     cashflow     end-balance     partial")
    for date in dates[1:]:
        cur_bal = float(periods[date][0])
        cashflow = float(periods[date][1])
        partial = 1.0
        # cashflow occurs on end date, so remove it from the current balance
        if last != 0:
            partial = 1 + ((cur_bal - cashflow) - last) / last
        if debug:
            print(f"{date.strftime('%Y-%m-%d')}  {last:-15.2f}  {cashflow:-11.2f}  {cur_bal:-14.2f}  {partial:-10.2f}")
        mean *= partial
        last = cur_bal
    mean = mean - 1.0
    days = (dates[-1] - dates[0]).days
    if days == 0:
        return 0.0
    twrr = (1 + mean) ** (365.0 / days) - 1
    return twrr

def fmt_d(num):
    """Decimal formatter"""
    return f'${num:,.0f}'

def fmt_pct(num):
    """Percent formatter"""
    return f'{num*100:.2f}%'

def add_position(position, inventory):
    """Add a posting to the inventory"""
    if isinstance(position, beancount.core.data.Posting):
        inventory.add_position(position)
    elif isinstance(position, beancount.core.data.TxnPosting):
        inventory.add_position(position.posting)
    else:
        raise Exception("Not a Posting or TxnPosting", position)


class IRR:
    """Wrapper class to allow caching results of multiple calculations to improve performance"""
    # pylint: disable=too-many-instance-attributes
    def __init__(self, entries, price_map, currency, errors=None):
        self.all_entries = entries
        self.price_map = price_map
        self.currency = currency
        self.market_value = {}
        self.times = [0, 0, 0, 0, 0, 0, 0]
        # The following reset after each calculate call()
        self.remaining = collections.deque()
        self.inventory = beancount.core.inventory.Inventory()
        self.interesting = {}
        self.internal = {}
        self.patterns = None
        self.internal_patterns = None
        self.errors = errors

    def _error(self, msg, meta=None):
        if self.errors:
            if not any(_.source == meta and _.message == msg and _.entry == None for _ in self.errors):
                self.errors.append(BeancountError(meta, msg, None))

    def elapsed(self):
        """Elapsed time of all runs of calculate()"""
        return sum(self.times)

    def iter_interesting_postings(self, date, entries):
        """Iterator for 'interesting' postings up-to a specified date"""
        if entries:
            remaining_postings = collections.deque(entries)
        else:
            remaining_postings = self.remaining
        while remaining_postings:
            entry = remaining_postings.popleft()
            if entry.date > date:
                remaining_postings.appendleft(entry)
                break
            for _p in entry.postings:
                if self.is_interesting_posting(_p):
                    yield _p

    def get_inventory_as_of_date(self, date, postings):
        """Get postings up-to a specified date"""
        if postings:
            inventory = beancount.core.inventory.Inventory()
        else:
            inventory = self.inventory
        for _p in self.iter_interesting_postings(date, postings):
            add_position(_p, inventory)
        return inventory

    def get_value_as_of(self, postings, date):
        """Get balance for a list of postings at a specified date"""
        inventory = self.get_inventory_as_of_date(date, postings)
        #balance = inventory.reduce(beancount.core.convert.convert_position, self.currency, self.price_map, date)
        balance = beancount.core.inventory.Inventory()
        if date not in self.market_value:
            self.market_value[date] = {}
        date_cache = self.market_value[date]
        for position in inventory:
            value = date_cache.get(position)
            if not value:
                value = beancount.core.convert.convert_position(position, self.currency, self.price_map, date)
                if value.currency != self.currency:
                    # try to convert position via cost
                    if position.cost and position.cost.currency == self.currency:
                        value = beancount.core.amount.Amount(position.cost.number * position.units.number,
                                                             self.currency)
                    else:
                        continue
                date_cache[position] = value
            balance.add_amount(value)
        amount = balance.get_currency_units(self.currency)
        return amount.number

    def is_interesting_posting(self, posting):
        """ Is this posting for an account we care about? """
        if posting.account not in self.interesting:
            self.interesting[posting.account] = bool(self.patterns.search(posting.account))
        return self.interesting[posting.account]

    def is_internal_account(self, posting):
        """ Is this an internal account that should be ignored? """
        if posting.account not in self.internal:
            self.internal[posting.account] = bool(self.internal_patterns.search(posting.account))
        return self.internal[posting.account]

    def is_interesting_entry(self, entry):
        """ Do any of the postings link to any of the accounts we care about? """
        for posting in entry.postings:
            if self.is_interesting_posting(posting):
                return True
        return False

    def calculate(self, patterns, internal_patterns=None, start_date=None, end_date=None,
                  mwr=True, twr=False,
                  cashflows=None, inflow_accounts=None, outflow_accounts=None,
                  debug_twr=False):
        """Calulate MWRR or TWRR for a set of accounts"""
        ## pylint: disable=too-many-branches too-many-statements too-many-locals too-many-arguments
        self.interesting.clear()
        self.internal.clear()
        self.inventory.clear()
        if cashflows is None:
            cashflows = []
        if inflow_accounts is None:
            inflow_accounts = set()
        if outflow_accounts is None:
            outflow_accounts = set()
        if not start_date:
            start_date = datetime.date.min
        if not end_date:
            end_date = datetime.date.today()
        elapsed = [0, 0, 0, 0, 0, 0, 0, 0]
        elapsed[0] = time.time()
        if internal_patterns:
            self.internal_patterns = re.compile(fr'^(?:{ "|".join(internal_patterns) })$')
        else:
            self.internal_patterns = re.compile('^$')
       
        self.patterns = re.compile(fr'^(?:{ "|".join(patterns) })$')

        elapsed[1] = time.time()
        only_txns = beancount.core.data.filter_txns(self.all_entries)
        elapsed[2] = time.time()
        interesting_txns = filter(self.is_interesting_entry, only_txns)
        elapsed[3] = time.time()
        # pull it into a list, instead of an iterator, because we're going to reuse it several times
        interesting_txns = list(interesting_txns)
        self.remaining = collections.deque(interesting_txns)
        twrr_periods = {}

        #p1 = get_inventory_as_of_date(datetime.date(2000, 3, 31), interesting_txns)
        #p2 = get_inventory_as_of_date(datetime.date(2000, 4, 17), interesting_txns)
        #p1a = get_inventory_as_of_date(datetime.date(2000, 3, 31), None)
        #p2a = get_inventory_as_of_date(datetime.date(2000, 4, 17), None)

        for entry in interesting_txns:
            if not start_date <= entry.date <= end_date:
                continue

            cashflow = Decimal(0)
            # Imagine an entry that looks like
            # [Posting(account=Assets:Brokerage, amount=100),
            #  Posting(account=Income:Dividend, amount=-100)]
            # We want that to net out to $0
            # But an entry like
            # [Posting(account=Assets:Brokerage, amount=100),
            #  Posting(account=Assets:Bank, amount=-100)]
            # should net out to $100
            # we loop over all postings in the entry. if the posting
            # is for an account we care about e.g. Assets:Brokerage then
            # we track the cashflow. But we *also* look for "internal"
            # cashflows and subtract them out. This will leave a net $0
            # if all the cashflows are internal.
            for posting in entry.postings:
                # convert_position uses the price-map to do price conversions, but this does not necessarily
                # accurately represent the cost at transaction time (due to intra-day variations).  That
                # could cause inacuracy, but since the cashflow is applied to the daily balance, it is more
                # important to be consistent with values
                converted = beancount.core.convert.convert_position(
                    posting, self.currency, self.price_map, entry.date)
                if converted.currency != self.currency:
                    # If the price_map does not contain a valid price, see if it can be calculated from cost
                    # This must align with get_value_as_of()
                    if posting.cost and posting.cost.currency == self.currency:
                        value = posting.cost.number * posting.units.number
                    else:
                        logging.error(f'Could not convert posting {converted} from {entry.date} at '
                                      f'{posting.meta["filename"]}:{posting.meta["lineno"]} to {self.currency}. '
                                       'IRR will be wrong.')
                        self._error(
                            f"Could not convert posting {converted} from {entry.date}, IRR will be wrong",
                            posting.meta)
                        continue
                else:
                    value = converted.number

                if self.is_interesting_posting(posting):
                    cashflow += value
                elif self.is_internal_account(posting):
                    cashflow += value
                else:
                    if value > 0:
                        outflow_accounts.add(posting.account)
                    else:
                        inflow_accounts.add(posting.account)
            # calculate net cashflow & the date
            if cashflow.quantize(Decimal('.01')) != 0:
                cashflows.append((entry.date, cashflow))
                if twr:
                    if entry.date not in twrr_periods:
                        twrr_periods[entry.date] = [self.get_value_as_of(None, entry.date), 0]
                    twrr_periods[entry.date][1] += cashflow

        elapsed[4] = time.time()
        start_value = self.get_value_as_of(interesting_txns, start_date)
        if start_date not in twrr_periods and start_date != datetime.date.min:
            twrr_periods[start_date] = [start_value, 0]  # We want the after-cashflow value
        # the start_value will include any cashflows that occurred on that date...
        # this leads to double-counting them, since they'll also appear in our cashflows
        # list. So we need to deduct them from start_value
        opening_txns = [amount for (date, amount) in cashflows if date == start_date]
        start_value -= functools.reduce(operator.add, opening_txns, 0)
        end_value = self.get_value_as_of(None, end_date)
        if end_date not in twrr_periods:
            twrr_periods[end_date] = [end_value, 0]
        # if starting balance isn't $0 at starting time period then we need a cashflow
        if start_value != 0:
            cashflows.insert(0, (start_date, start_value))
        # if ending balance isn't $0 at end of time period then we need a cashflow
        if end_value != 0:
            cashflows.append((end_date, -end_value))
        irr = None
        twrr = None
        elapsed[5] = time.time()
        if mwr:
            if cashflows:
                # we need to coerce everything to a float for xirr to work...
                irr = xirr([(d, float(f)) for (d,f) in cashflows])
            else:
                logging.error(f'No cashflows found during the time period {start_date} -> {end_date}')
        elapsed[6] = time.time()
        if twr and twrr_periods:
            twrr = xtwrr(twrr_periods, debug=debug_twr)
        elapsed[7] = time.time()
        for i in range(7):
            delta = elapsed[i+1] - elapsed[i]
            self.times[i] += delta
            # print(f"T{i}: delta")
        return irr, twrr

def main():
    """Entrypoint"""
    ## pylint: disable=too-many-branches too-many-statements
    logging.basicConfig(format='%(levelname)s: %(message)s')
    parser = argparse.ArgumentParser(
        description="Calculate return data."
    )
    parser.add_argument('bean', help='Path to the beancount file.')
    parser.add_argument('--currency', default='USD', help='Currency to use for calculating returns.')
    parser.add_argument('--account', action='append', default=[],
        help='Regex pattern of accounts to include when calculating returns. Can be specified multiple times.')
    parser.add_argument('--internal', action='append', default=[],
        help='Regex pattern of accounts that represent internal cashflows (i.e. dividends or interest)')

    parser.add_argument('--from', dest='date_from', type=lambda d: datetime.datetime.strptime(d, '%Y-%m-%d').date(),
        help='Start date: YYYY-MM-DD, 2016-12-31')
    parser.add_argument('--to', dest='date_to', type=lambda d: datetime.datetime.strptime(d, '%Y-%m-%d').date(),
        help='End date YYYY-MM-DD, 2016-12-31')

    date_range = parser.add_mutually_exclusive_group()
    date_range.add_argument('--year', default=False, type=int, help='Year. Shorthand for --from/--to.')
    date_range.add_argument('--ytd', action='store_true')
    date_range.add_argument('--1year', action='store_true')
    date_range.add_argument('--2year', action='store_true')
    date_range.add_argument('--3year', action='store_true')
    date_range.add_argument('--5year', action='store_true')
    date_range.add_argument('--10year', action='store_true')

    parser.add_argument('--debug-inflows', action='store_true',
        help='Print list of all inflow accounts in transactions.')
    parser.add_argument('--debug-outflows', action='store_true',
        help='Print list of all outflow accounts in transactions.')
    parser.add_argument('--debug-cashflows', action='store_true',
        help='Print list of all cashflows used for the IRR calculation.')
    parser.add_argument('--debug-twr', action='store_true',
        help='Print calculations for TWR.')

    args = parser.parse_args()

    shortcuts = ['year', 'ytd', '1year', '2year', '3year', '5year', '10year']
    shortcut_used = functools.reduce(operator.__or__, [getattr(args, x) for x in shortcuts])
    if shortcut_used and (args.date_from or args.date_to):
        raise Exception('Date shortcut options mutually exclusive with --to/--from options')

    if args.year:
        args.date_from = datetime.date(args.year, 1, 1)
        args.date_to = datetime.date(args.year, 12, 31)

    if args.ytd:
        today = datetime.date.today()
        args.date_from = datetime.date(today.year, 1, 1)
        args.date_to = today

    if getattr(args, '1year'):
        today = datetime.date.today()
        args.date_from = today + relativedelta(years=-1)
        args.date_to = today

    if getattr(args, '2year'):
        today = datetime.date.today()
        args.date_from = today + relativedelta(years=-2)
        args.date_to = today

    if getattr(args, '3year'):
        today = datetime.date.today()
        args.date_from = today + relativedelta(years=-3)
        args.date_to = today

    if getattr(args, '5year'):
        today = datetime.date.today()
        args.date_from = today + relativedelta(years=-5)
        args.date_to = today

    if getattr(args, '10year'):
        today = datetime.date.today()
        args.date_from = today + relativedelta(years=-10)
        args.date_to = today

    entries, _errors, _options = beancount.loader.load_file(args.bean, logging.info, log_errors=sys.stderr)
    price_map = beancount.core.prices.build_price_map(entries)

    cashflows = []
    inflow_accounts = set()
    outflow_accounts = set()
    irr, twr = IRR(entries, price_map, args.currency).calculate(
        args.account, internal_patterns=args.internal, start_date=args.date_from, end_date=args.date_to,
        mwr=True, twr=True,
        cashflows=cashflows, inflow_accounts=inflow_accounts, outflow_accounts=outflow_accounts,
        debug_twr=args.debug_twr)
    if irr:
        print(f"IRR: {irr}")
    if twr:
        print(f"TWR: {twr}")
    if args.debug_cashflows:
        pprint(cashflows)
    if args.debug_inflows:
        print('>> [inflows]')
        pprint(inflow_accounts)
    if args.debug_outflows:
        print('<< [outflows]')
        pprint(outflow_accounts)

if __name__ == '__main__':
    main()
