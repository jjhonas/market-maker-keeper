# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import itertools
import logging
import operator
from functools import reduce
from pprint import pformat

from market_maker_keeper.reloadable_config import ReloadableConfig
from pymaker.numeric import Wad


class Band:
    def __init__(self,
                 min_margin: float,
                 avg_margin: float,
                 max_margin: float,
                 min_amount: Wad,
                 avg_amount: Wad,
                 max_amount: Wad,
                 dust_cutoff: Wad):
        assert(isinstance(min_margin, float))
        assert(isinstance(avg_margin, float))
        assert(isinstance(max_margin, float))
        assert(isinstance(min_amount, Wad))
        assert(isinstance(avg_amount, Wad))
        assert(isinstance(max_amount, Wad))
        assert(isinstance(dust_cutoff, Wad))

        self.min_margin = min_margin
        self.avg_margin = avg_margin
        self.max_margin = max_margin
        self.min_amount = min_amount
        self.avg_amount = avg_amount
        self.max_amount = max_amount
        self.dust_cutoff = dust_cutoff

        assert(self.min_amount <= self.avg_amount)
        assert(self.avg_amount <= self.max_amount)
        assert(self.min_margin <= self.avg_margin)
        assert(self.avg_margin <= self.max_margin)
        assert(self.min_margin < self.max_margin)

    def includes(self, order, target_price: Wad) -> bool:
        raise NotImplemented()

    def excessive_orders(self, orders: list, target_price: Wad):
        """Return orders which need to be cancelled to bring the total order amount in the band below maximum."""
        orders_in_band = [order for order in orders if self.includes(order, target_price)]
        if self._total_amount(orders_in_band) > self.max_amount:
            def calculate_all_subsets():
                for num in range(0, len(orders_in_band)):
                    for combination in itertools.combinations(orders_in_band, num):
                        yield set(combination)

            # all possible subsets of orders which can be left uncancelled, including the empty subset
            all_subsets = list(calculate_all_subsets())

            # we are only choosing from these subsets which bring us to or below `band.max_amount`
            candidate_subsets = list(filter(lambda subset: self._total_amount(subset) <= self.max_amount, all_subsets))

            # we calculate the size of the largest subset of these, as this will result in the lowest number
            # of order cancellations i.e. lowest gas consumption for the keeper
            #
            # then we only limit interesting subsets to the ones of that size, ignoring smaller ones
            highest_cnt = max(map(lambda subset: len(subset), candidate_subsets))
            candidate_subsets = filter(lambda subset: len(subset) == highest_cnt, candidate_subsets)

            # from the interesting subsets we choose the with the highest total amount
            found_subset = sorted(candidate_subsets, key=lambda subset: self._total_amount(subset), reverse=True)[0]

            # as we are supposed to return the orders which should be cancelled, we return the complement
            # of the found subset
            return set(orders_in_band) - set(found_subset)
        else:
            return []

    @staticmethod
    def _total_amount(orders: list):
        return reduce(operator.add, map(lambda order: order.remaining_sell_amount, orders), Wad(0))


class BuyBand(Band):
    def __init__(self, dictionary: dict):
        super().__init__(min_margin=dictionary['minMargin'],
                         avg_margin=dictionary['avgMargin'],
                         max_margin=dictionary['maxMargin'],
                         min_amount=Wad.from_number(dictionary['minSaiAmount']) if 'minSaiAmount' in dictionary else Wad.from_number(dictionary['minAmount']),
                         avg_amount=Wad.from_number(dictionary['avgSaiAmount']) if 'avgSaiAmount' in dictionary else Wad.from_number(dictionary['avgAmount']),
                         max_amount=Wad.from_number(dictionary['maxSaiAmount']) if 'maxSaiAmount' in dictionary else Wad.from_number(dictionary['maxAmount']),
                         dust_cutoff=Wad.from_number(dictionary['dustCutoff']))

    def includes(self, order, target_price: Wad) -> bool:
        price = order.sell_to_buy_price
        price_min = self._apply_margin(target_price, self.min_margin)
        price_max = self._apply_margin(target_price, self.max_margin)
        return (price > price_max) and (price <= price_min)

    def avg_price(self, target_price: Wad) -> Wad:
        return self._apply_margin(target_price, self.avg_margin)

    @staticmethod
    def _apply_margin(price: Wad, margin: float) -> Wad:
        return price * Wad.from_number(1 - margin)


class SellBand(Band):
    def __init__(self, dictionary: dict):
        super().__init__(min_margin=dictionary['minMargin'],
                         avg_margin=dictionary['avgMargin'],
                         max_margin=dictionary['maxMargin'],
                         min_amount=Wad.from_number(dictionary['minWEthAmount']) if 'minWEthAmount' in dictionary else Wad.from_number(dictionary['minAmount']),
                         avg_amount=Wad.from_number(dictionary['avgWEthAmount']) if 'avgWEthAmount' in dictionary else Wad.from_number(dictionary['avgAmount']),
                         max_amount=Wad.from_number(dictionary['maxWEthAmount']) if 'maxWEthAmount' in dictionary else Wad.from_number(dictionary['maxAmount']),
                         dust_cutoff=Wad.from_number(dictionary['dustCutoff']))

    def includes(self, order, target_price: Wad) -> bool:
        price = order.buy_to_sell_price
        price_min = self._apply_margin(target_price, self.min_margin)
        price_max = self._apply_margin(target_price, self.max_margin)
        return (price > price_min) and (price <= price_max)

    def avg_price(self, target_price: Wad) -> Wad:
        return self._apply_margin(target_price, self.avg_margin)

    @staticmethod
    def _apply_margin(price: Wad, margin: float) -> Wad:
        return price * Wad.from_number(1 + margin)


class NewOrder:
    def __init__(self, is_sell: bool, price: Wad, pay_amount: Wad, buy_amount: Wad):
        assert(isinstance(is_sell, bool))
        assert(isinstance(price, Wad))
        assert(isinstance(pay_amount, Wad))
        assert(isinstance(buy_amount, Wad))

        self.is_sell = is_sell
        self.price = price
        self.pay_amount = pay_amount
        self.buy_amount = buy_amount

    def __repr__(self):
        return pformat(vars(self))


class Bands:
    logger = logging.getLogger()

    def __init__(self, reloadable_config: ReloadableConfig):
        assert(isinstance(reloadable_config, ReloadableConfig))

        config = reloadable_config.get_config()
        self.buy_bands = list(map(BuyBand, config['buyBands']))
        self.sell_bands = list(map(SellBand, config['sellBands']))

        if self._bands_overlap(self.buy_bands) or self._bands_overlap(self.sell_bands):
            raise Exception(f"Bands in the config file overlap")

    def excessive_sell_orders(self, our_sell_orders: list, target_price: Wad):
        """Return sell orders which need to be cancelled to bring total amounts within all sell bands below maximums."""
        assert(isinstance(our_sell_orders, list))
        assert(isinstance(target_price, Wad))

        for band in self.sell_bands:
            for order in band.excessive_orders(our_sell_orders, target_price):
                yield order

    def excessive_buy_orders(self, our_buy_orders: list, target_price: Wad):
        """Return buy orders which need to be cancelled to bring total amounts within all buy bands below maximums."""
        assert(isinstance(our_buy_orders, list))
        assert(isinstance(target_price, Wad))

        for band in self.buy_bands:
            for order in band.excessive_orders(our_buy_orders, target_price):
                yield order

    def outside_orders(self, our_buy_orders: list, our_sell_orders: list, target_price: Wad):
        """Return orders which do not fall into any buy or sell band."""
        def outside_any_band_orders(orders: list, bands: list):
            for order in orders:
                if not any(band.includes(order, target_price) for band in bands):
                    yield order

        return itertools.chain(outside_any_band_orders(our_buy_orders, self.buy_bands),
                               outside_any_band_orders(our_sell_orders, self.sell_bands))

    def new_sell_orders(self, our_sell_orders: list, our_sell_balance: Wad, target_price: Wad):
        """Return sell orders which need to be placed to bring total amounts within all sell bands above minimums."""
        assert(isinstance(our_sell_orders, list))
        assert(isinstance(our_sell_balance, Wad))
        assert(isinstance(target_price, Wad))

        for band in self.sell_bands:
            orders = [order for order in our_sell_orders if band.includes(order, target_price)]
            total_amount = self._total_amount(orders)
            if total_amount < band.min_amount:
                price = band.avg_price(target_price)
                pay_amount = Wad.min(band.avg_amount - total_amount, our_sell_balance)
                buy_amount = pay_amount * price
                if (pay_amount >= band.dust_cutoff) and (pay_amount > Wad(0)) and (buy_amount > Wad(0)):
                    self.logger.debug(f"Using price {price} for new sell order")

                    our_sell_balance = our_sell_balance - buy_amount
                    yield NewOrder(is_sell=True, price=price, pay_amount=pay_amount, buy_amount=buy_amount)

    def new_buy_orders(self, our_buy_orders: list, our_buy_balance: Wad, target_price: Wad):
        """Return buy orders which need to be placed to bring total amounts within all buy bands above minimums."""
        assert(isinstance(our_buy_orders, list))
        assert(isinstance(our_buy_balance, Wad))
        assert(isinstance(target_price, Wad))

        for band in self.buy_bands:
            orders = [order for order in our_buy_orders if band.includes(order, target_price)]
            total_amount = self._total_amount(orders)
            if total_amount < band.min_amount:
                price = band.avg_price(target_price)
                pay_amount = Wad.min(band.avg_amount - total_amount, our_buy_balance)
                buy_amount = pay_amount / price
                if (pay_amount >= band.dust_cutoff) and (pay_amount > Wad(0)) and (buy_amount > Wad(0)):
                    self.logger.debug(f"Using price {price} for new buy order")

                    our_buy_balance = our_buy_balance - pay_amount
                    yield NewOrder(is_sell=False, price=price, pay_amount=pay_amount, buy_amount=buy_amount)

    @staticmethod
    def _total_amount(orders):
        return reduce(operator.add, map(lambda order: order.remaining_sell_amount, orders), Wad(0))

    @staticmethod
    def _bands_overlap(bands: list):
        def two_bands_overlap(band1, band2):
            return band1.min_margin < band2.max_margin and band2.min_margin < band1.max_margin

        for band1 in bands:
            if len(list(filter(lambda band2: two_bands_overlap(band1, band2), bands))) > 1:
                return True

        return False
