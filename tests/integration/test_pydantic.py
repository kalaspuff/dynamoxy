"""Pydantic models on every backend: equality, persistence state and existing models."""

from __future__ import annotations

import pytest

import dynamoxy
from tests.models import Account, Contact, ContactRecord


async def test_loaded_models_compare_by_value(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    accounts = client.model(Account)
    created = await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    loaded = await accounts.get_or_raise("a1")
    assert loaded.is_loaded
    assert loaded == created
    assert loaded == Account(account_id="a1", owner="Ada", handle="ada", version=1), "being loaded is not a value"
    assert loaded != Account(account_id="a1", owner="Bo", handle="ada", version=1)
    loaded.owner = "Bo"  # pydantic validates the assignment; the persistence state stays
    assert loaded.is_loaded
    assert set(loaded.model_dump()) == {"account_id", "owner", "handle", "balance", "version"}
    await accounts.save(loaded)  # optimistic locking still knows the stored version
    assert loaded.version == 2


async def test_existing_models_through_a_subclass(client: dynamoxy.Client) -> None:
    await client.create_tables(ContactRecord)
    contacts = client.model(ContactRecord)
    existing = Contact(contact_id="c1", email="ada@example.com", name="Ada")
    await contacts.create(ContactRecord.model_validate(existing.model_dump()))
    loaded = await contacts.get_or_raise("c1")
    assert isinstance(loaded, Contact)
    assert loaded == ContactRecord.model_validate(existing.model_dump())
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await contacts.create(ContactRecord(contact_id="c2", email="ada@example.com", name="Bo"))


async def test_model_copies_track_their_own_stored_item(client: dynamoxy.Client) -> None:
    await client.create_tables(Account)
    accounts = client.model(Account)
    ada = await accounts.create(Account(account_id="a1", owner="Ada", handle="ada"))
    bob = ada.model_copy(update={"account_id": "a2", "owner": "Bob", "handle": "bob", "version": None})
    await accounts.create(bob)
    # The guard transition of `ada` uses what `ada` knows about its stored item, not `bob`'s.
    await accounts.update(ada, dynamoxy.attr(Account.handle).set("ada2"))
    assert (await accounts.get_or_raise("a1")).handle == "ada2"
    await accounts.create(Account(account_id="a3", owner="Cy", handle="ada"))  # free again
    with pytest.raises(dynamoxy.UniqueConstraintError):
        await accounts.create(Account(account_id="a4", owner="Di", handle="bob"))  # still taken
