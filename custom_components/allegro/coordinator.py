"""Data update coordinator for Allegro."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AllegroApiClient, AllegroApiError, AllegroAuthError
from .const import (
    CONF_AUTO_READ,
    CONF_AUTO_READ_DAYS,
    CONF_DEFAULT_PICKUP_POINT,
    DEFAULT_AUTO_READ_DAYS,
    DOMAIN,
)
from .messages import Attachment, Messages, MessageThread
from .models import AllegroData, Cart, groups_with_returns
from .returns import Return

_LOGGER = logging.getLogger(__package__)
SCAN_INTERVAL = timedelta(minutes=30)
MAX_AUTO_READ = 10
MAX_BULK_READ = 50
# A single 401/403 can be a hiccup or a bot check; several in a row mean a dead session.
AUTH_FAILURES_BEFORE_REAUTH = 3
# Returns outlive the 25 newest orders the sensors look at, so the returns
# scan reads further back. Finished returns stay listed for a while.
RETURNS_SCAN_LIMIT = 100
FINISHED_RETURNS_DAYS = 30

type AllegroConfigEntry = ConfigEntry["AllegroCoordinator"]


class AllegroCoordinator(DataUpdateCoordinator[AllegroData]):
    """Fetch and cache Allegro orders, cart and messages."""

    config_entry: AllegroConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AllegroConfigEntry,
        client: AllegroApiClient,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.api = client
        self._auth_failures = 0
        self.last_success: datetime | None = None
        # Finished returns do not change any more, so they are fetched once.
        self._finished_returns: dict[str, tuple[Return, ...]] = {}

    @property
    def default_pickup_point(self) -> str:
        """Code of the user's usual parcel locker, for example `WAW01A`."""
        value = self.config_entry.options.get(CONF_DEFAULT_PICKUP_POINT) or ""
        return str(value).strip()

    async def _async_update_data(self) -> AllegroData:
        """Fetch orders, cart and messages from Allegro."""
        orders, cart_result, messages_result, packages_result = await asyncio.gather(
            self.api.async_get_orders(),
            self.api.async_get_cart(),
            self.api.async_get_messages(),
            self.api.async_get_packages(dt_util.get_default_time_zone()),
            return_exceptions=True,
        )
        if isinstance(orders, AllegroAuthError):
            self._auth_failures += 1
            # At startup there is no earlier data to fall back on: ask right away.
            if self.data is None or self._auth_failures >= AUTH_FAILURES_BEFORE_REAUTH:
                raise ConfigEntryAuthFailed(str(orders)) from orders
            raise UpdateFailed(str(orders)) from orders
        if isinstance(orders, BaseException):
            if isinstance(orders, AllegroApiError):
                raise UpdateFailed(str(orders)) from orders
            raise orders
        self._auth_failures = 0
        self.last_success = dt_util.utcnow()
        if isinstance(cart_result, AllegroApiError):
            _LOGGER.warning("Could not refresh Allegro cart: %s", cart_result)
            cart = self.data.cart if self.data is not None else Cart.empty()
        elif isinstance(cart_result, BaseException):
            raise cart_result
        else:
            cart = cart_result
        if isinstance(messages_result, AllegroApiError):
            _LOGGER.warning("Could not refresh Allegro messages: %s", messages_result)
            messages = self.data.messages if self.data is not None else Messages()
        elif isinstance(messages_result, BaseException):
            raise messages_result
        else:
            messages = messages_result
        if isinstance(packages_result, AllegroApiError):
            _LOGGER.warning("Could not refresh Allegro packages: %s", packages_result)
            packages = self.data.packages if self.data is not None else ()
        elif isinstance(packages_result, BaseException):
            raise packages_result
        else:
            packages = tuple(packages_result)
        data = AllegroData(
            orders=orders,
            cart=cart,
            messages=messages,
            packages=packages,
            default_pickup_point=self.default_pickup_point,
        )
        data = replace(data, returns=await self._async_fetch_returns(data))
        return await self._async_auto_read(data)

    async def _async_fetch_returns(self, data: AllegroData) -> tuple[Return, ...]:
        """Fetch the returns filed for recent orders.

        Looks further back than the sensors' order list, and keeps following
        open returns from earlier updates. A group that fails keeps the
        returns from the previous update.
        """
        previous = self.data.returns if self.data is not None else ()
        orders = list(data.orders)
        try:
            orders += await self.api.async_get_orders(RETURNS_SCAN_LIMIT)
        except AllegroApiError as err:
            _LOGGER.warning("Could not scan older Allegro orders for returns: %s", err)
        groups = groups_with_returns(orders)
        groups += [
            group
            for group in dict.fromkeys(
                item.group_id for item in previous if item.is_active
            )
            if group not in groups
        ]
        to_fetch = [group for group in groups if group not in self._finished_returns]
        results = await asyncio.gather(
            *(self.api.async_get_returns(group) for group in to_fetch),
            return_exceptions=True,
        )
        fetched: dict[str, tuple[Return, ...]] = {}
        for group, result in zip(to_fetch, results, strict=True):
            if isinstance(result, AllegroApiError):
                _LOGGER.warning(
                    "Could not refresh Allegro return %s: %s", group, result
                )
                fetched[group] = tuple(
                    item for item in previous if item.group_id == group
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                fetched[group] = tuple(result)
                if result and not any(item.is_active for item in result):
                    self._finished_returns[group] = tuple(result)

        # Forget finished returns that dropped out of the scanned orders.
        self._finished_returns = {
            group: items
            for group, items in self._finished_returns.items()
            if group in groups
        }
        cutoff = dt_util.utcnow() - timedelta(days=FINISHED_RETURNS_DAYS)
        returns: list[Return] = []
        for group in groups:
            for item in self._finished_returns.get(group) or fetched.get(group, ()):
                if (
                    item.is_active
                    or item.requested_at is None
                    or item.requested_at >= cutoff
                ):
                    returns.append(item)
        return tuple(returns)

    def _older_threads(self, data: AllegroData) -> list[MessageThread]:
        """Unread automatic order messages that no longer need attention."""
        days = float(
            self.config_entry.options.get(CONF_AUTO_READ_DAYS) or DEFAULT_AUTO_READ_DAYS
        )
        return data.messages.auto_read_candidates(
            data.active_sellers, days, dt_util.utcnow()
        )

    async def _async_auto_read(self, data: AllegroData) -> AllegroData:
        """Mark automatic order messages as read when they are no longer useful."""
        if not self.config_entry.options.get(CONF_AUTO_READ):
            return data
        candidates = self._older_threads(data)
        read: set[str] = set()
        for thread in candidates[:MAX_AUTO_READ]:
            try:
                await self.api.async_open_thread(thread.id)
            except AllegroApiError as err:
                _LOGGER.warning("Could not mark message as read: %s", err)
                break
            read.add(thread.id)
        if not read:
            return data
        _LOGGER.debug("Marked %d automatic Allegro messages as read", len(read))
        return replace(data, messages=data.messages.with_read(read))

    def _apply_read(self, thread_ids: set[str]) -> None:
        if self.data is None or not thread_ids:
            return
        self.async_set_updated_data(
            replace(self.data, messages=self.data.messages.with_read(thread_ids))
        )

    async def async_open_thread(self, thread_id: str) -> list[Attachment]:
        """Open a thread, which marks it as read, and return its attachments."""
        attachments = await self.api.async_open_thread(thread_id)
        self._apply_read({thread_id})
        return attachments

    async def async_mark_threads_read(self, category: str) -> int:
        """Mark unread threads of a category as read. Returns how many were read."""
        threads = (
            self.data.messages.unread_in_category(category)
            if self.data is not None
            else []
        )
        read: set[str] = set()
        try:
            for thread in threads[:MAX_BULK_READ]:
                await self.api.async_open_thread(thread.id)
                read.add(thread.id)
        finally:
            self._apply_read(read)
        return len(read)

    async def async_mark_older_read(self) -> int:
        """Mark older automatic order messages as read. Returns how many."""
        if self.data is None:
            return 0
        read: set[str] = set()
        try:
            for thread in self._older_threads(self.data)[:MAX_BULK_READ]:
                await self.api.async_open_thread(thread.id)
                read.add(thread.id)
        finally:
            self._apply_read(read)
        return len(read)

    async def async_add_to_cart(self, item_id: str, quantity: int = 1) -> None:
        """Add an offer to the cart and refresh cart state."""
        await self.api.async_add_to_cart(item_id, quantity)
        try:
            cart = await self.api.async_get_cart()
        except AllegroApiError as err:
            _LOGGER.warning("Added to cart but failed to refresh cart: %s", err)
            await self.async_request_refresh()
            return
        if self.data is None:
            await self.async_request_refresh()
            return
        self.async_set_updated_data(replace(self.data, cart=cart))
