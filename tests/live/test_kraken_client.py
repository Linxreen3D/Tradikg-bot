import base64
import json
import os
import stat

import pytest

from tradikg_bot.live.kraken.client import (
    ForbiddenOperationError,
    HttpResponse,
    KrakenClient,
    KrakenRejectedError,
    KrakenUnknownOutcomeError,
    sign,
)
from tradikg_bot.live.kraken.credentials import (
    CredentialsError,
    KrakenCredentials,
    load_credentials,
)

from live_helpers import DOC_SECRET, DOC_SIGNATURE


def test_signature_matches_official_kraken_example():
    data = {
        "nonce": 1616492376594,
        "ordertype": "limit",
        "pair": "XBTUSD",
        "price": 37500,
        "type": "buy",
        "volume": 1.25,
    }
    assert sign("/0/private/AddOrder", data, DOC_SECRET) == DOC_SIGNATURE


class Recorder:
    def __init__(self, status=200, payload=None, exc=None):
        self.calls = []
        self.status, self.payload, self.exc = status, payload or {"error": [], "result": {}}, exc

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if self.exc:
            raise self.exc
        return HttpResponse(self.status, json.dumps(self.payload).encode())


def client(transport, allow_trading=False):
    nonces = iter(range(1000, 2000))
    creds = KrakenCredentials("key", DOC_SECRET)
    return KrakenClient(creds, lambda: next(nonces), transport, allow_trading=allow_trading)


@pytest.mark.parametrize(
    "endpoint",
    [
        "Withdraw",
        "WithdrawCancel",
        "WalletTransfer",
        "DepositAddresses",
        "Stake",
        "EditOrder",
        "CancelAll",
        "AddOrderBatch",
        "AmendOrder",
    ],
)
def test_money_movement_and_unknown_endpoints_are_unreachable(endpoint):
    rec = Recorder()
    with pytest.raises(ForbiddenOperationError):
        client(rec, allow_trading=True).private(endpoint, {"asset": "XBT"})
    assert rec.calls == []


def test_add_order_requires_trading_permission_but_validate_is_allowed():
    rec = Recorder(payload={"error": [], "result": {"descr": {"order": "buy"}}})
    c = client(rec, allow_trading=False)
    with pytest.raises(ForbiddenOperationError):
        c.private("AddOrder", {"ordertype": "limit", "pair": "XBTEUR", "volume": "1"})
    c.private(
        "AddOrder", {"ordertype": "limit", "pair": "XBTEUR", "volume": "1", "validate": "true"}
    )
    assert len(rec.calls) == 1 and b"validate=true" in rec.calls[0][3]


@pytest.mark.parametrize(
    "params",
    [
        {"ordertype": "limit", "leverage": "2"},
        {"ordertype": "limit", "reduce_only": "true"},
        {"ordertype": "settle-position"},
        {"ordertype": "limit", "close[ordertype]": "settle-position"},
    ],
)
def test_margin_and_leverage_parameters_are_blocked(params):
    rec = Recorder()
    with pytest.raises(ForbiddenOperationError):
        client(rec, allow_trading=True).private("AddOrder", params)
    assert rec.calls == []


def test_private_request_is_signed_and_nonce_increases():
    rec = Recorder()
    c = client(rec)
    c.private("Balance")
    c.private("Balance")
    (_, url, headers, body), (_, _, _, body2) = rec.calls
    assert url.endswith("/0/private/Balance")
    assert headers["API-Key"] == "key"
    assert headers["API-Sign"] == sign("/0/private/Balance", {"nonce": 1000}, DOC_SECRET)
    assert body == b"nonce=1000" and body2 == b"nonce=1001"


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        (["EOrder:Insufficient funds"], KrakenRejectedError),
        (["EOrder:Order minimum not met"], KrakenRejectedError),
        (["EGeneral:Permission denied"], KrakenRejectedError),
        (["EAPI:Invalid nonce"], KrakenRejectedError),
        (["EService:Unavailable"], KrakenUnknownOutcomeError),
        (["EService:Busy"], KrakenUnknownOutcomeError),
        (["EGeneral:Internal error"], KrakenUnknownOutcomeError),
    ],
)
def test_error_classification(errors, expected):
    rec = Recorder(payload={"error": errors})
    with pytest.raises(expected):
        client(rec).private("Balance")


def test_http_5xx_and_timeouts_are_unknown_outcome():
    with pytest.raises(KrakenUnknownOutcomeError):
        client(Recorder(status=502, payload={})).private("Balance")
    with pytest.raises(KrakenUnknownOutcomeError):
        client(Recorder(exc=KrakenUnknownOutcomeError("timeout"))).private("Balance")


def test_secrets_never_in_repr():
    c = client(Recorder())
    assert DOC_SECRET not in repr(c) and "key" not in repr(c).replace("KrakenClient", "")
    assert DOC_SECRET not in repr(KrakenCredentials("key", DOC_SECRET))


def test_credentials_from_env():
    creds = load_credentials({"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": DOC_SECRET})
    assert creds.api_key == "k"
    with pytest.raises(CredentialsError):
        load_credentials({}, keychain=lambda account: None)  # nie den echten Schlüsselbund lesen
    with pytest.raises(CredentialsError):
        load_credentials({"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "kein base64!"})


def test_credentials_file_must_be_private_and_outside_git(tmp_path):
    path = tmp_path / "kraken.json"
    path.write_text(json.dumps({"api_key": "k", "api_secret": DOC_SECRET}))
    env = {"TRADIKG_KRAKEN_CREDENTIALS": str(path)}

    def empty(_account):
        return None

    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(CredentialsError, match="lesbar"):
            load_credentials(env, keychain=empty)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert load_credentials(env, keychain=empty).api_key == "k"
    (tmp_path / ".git").mkdir()
    with pytest.raises(CredentialsError, match="Git"):
        load_credentials(env, keychain=empty)


def test_doc_secret_is_valid_base64():
    assert len(base64.b64decode(DOC_SECRET)) == 64


def test_credentials_from_macos_keychain():
    store = {"api-key": "k-from-keychain", "api-secret": DOC_SECRET}
    creds = load_credentials({}, keychain=store.get)
    assert creds.api_key == "k-from-keychain"
    with pytest.raises(CredentialsError, match="Schlüsselbund"):
        load_credentials({}, keychain=lambda account: None)


def test_env_is_fallback_and_keychain_wins_without_leaking_secrets(tmp_path, monkeypatch):
    import tradikg_bot.live.kraken.credentials as creds_mod

    env = {"KRAKEN_API_KEY": "from-env", "KRAKEN_API_SECRET": DOC_SECRET}
    creds = load_credentials(env, keychain=lambda account: None)
    assert creds.api_key == "from-env"
    assert DOC_SECRET not in repr(creds)

    path = tmp_path / "kraken.json"
    path.write_text(json.dumps({"api_key": "from-file", "api_secret": DOC_SECRET}))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    both = {**env, "TRADIKG_KRAKEN_CREDENTIALS": str(path)}
    chain = {"api-key": "from-chain", "api-secret": DOC_SECRET}
    assert load_credentials(both, keychain=chain.get).api_key == "from-chain"

    def incomplete(account):
        return "PARTIAL-KEY-VALUE" if account == "api-key" else None

    with pytest.raises(CredentialsError, match="unvollständig") as exc:
        load_credentials(both, keychain=incomplete)
    text = str(exc.value)
    assert "PARTIAL-KEY-VALUE" not in text
    assert DOC_SECRET not in text
    assert "from-env" not in text and "from-file" not in text

    monkeypatch.setattr(creds_mod.sys, "platform", "linux")

    def boom(_account):
        raise AssertionError("Schlüsselbund auf Linux aufgerufen")

    monkeypatch.setattr(creds_mod, "_keychain_lookup", boom)
    assert load_credentials(env).api_key == "from-env"

    monkeypatch.setattr(creds_mod.sys, "platform", "darwin")
    monkeypatch.setattr(creds_mod, "_keychain_lookup", chain.get)
    assert load_credentials(env).api_key == "from-chain"
    assert DOC_SECRET not in repr(load_credentials(env))
