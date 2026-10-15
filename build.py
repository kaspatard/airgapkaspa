"""Interactive unsigned Kaspa mainnet transaction builder.

Run on the online Windows computer. No seed phrase or private key is used here.
Requires `kaspa` and `requests` packages. Outputs `unsigned.json` beside this file.

IMPORTANT: The old kaspa_offline_sign.py and broadcast.py scripts from the first
69.69 KAS test were pinned to that specific transaction. They will REJECT new
transactions until replaced with general-purpose, independently verified scripts.
"""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import kaspa
import requests

SOMPI = 100_000_000
API = "https://api.kaspa.org"
OUTPUT_FILE = Path(__file__).resolve().with_name("unsigned.json")


def kas_amount(prompt):
    while True:
        raw = input(prompt).strip()
        try:
            amount = Decimal(raw)
            sompi = amount * SOMPI
            if not amount.is_finite() or amount <= 0 or sompi != sompi.to_integral_value():
                raise ValueError
            return int(sompi)
        except (InvalidOperation, ValueError, OverflowError):
            print("Enter a positive KAS amount with no more than 8 decimal places.")


def kas_address(prompt):
    while True:
        address = input(prompt).strip()
        try:
            if not address.startswith("kaspa:"):
                raise ValueError("Expected mainnet address beginning 'kaspa:'")
            kaspa.Address(address)  # check Kaspa address syntax/checksum
            return address
        except (ValueError, Exception) as exc:
            print(f"Invalid address: {exc}")


def abort(message):
    raise SystemExit("STOPPED: " + message)


def outpoint_key(outpoint):
    return (str(outpoint["transactionId"]).lower(), int(outpoint["index"]))


def script_key(spk):
    return (int(spk["version"]), str(spk["script"]).lower())


def expected_script(address):
    obj = kaspa.pay_to_address_script(kaspa.Address(address))
    return (int(obj.version), str(obj.script).lower())


def main():
    print("\n=== Kaspa mainnet: Build unsigned transaction ===")
    print("Only PUBLIC wallet addresses belong on this computer.\n")

    source = kas_address("Sending wallet (public Kaspa address): ")
    destination = kas_address("Recipient wallet (public Kaspa address): ")
    if source == destination:
        abort("Sender and recipient are the same. This simple builder expects two distinct addresses.")

    send = kas_amount("Amount to send (KAS): ")
    max_fee = kas_amount("Maximum fee (KAS, e.g. 0.01): ")
    change = source

    print("\nYour instructions:")
    print("  Source:      ", source)
    print("  Recipient:   ", destination)
    print(f"  Send:         {send / SOMPI:.8f} KAS")
    print(f"  Maximum fee:  {max_fee / SOMPI:.8f} KAS")
    print("  Change:      ", change)
    if input("\nType BUILD to continue: ").strip() != "BUILD":
        abort("Not approved")

    print("\nFetching current unspent outputs from Kaspa REST API...")
    response = requests.get(f"{API}/addresses/{source}/utxos", timeout=20)
    response.raise_for_status()
    raw_utxos = response.json()
    if not isinstance(raw_utxos, list) or not raw_utxos:
        abort("No UTXOs returned for source address")

    entries = []
    known = {}
    source_script = expected_script(source)

    for item in raw_utxos:
        u = item["utxoEntry"]
        spk = u["scriptPublicKey"]
        normalized = {
            "address": source,
            "outpoint": {
                "transactionId": item["outpoint"]["transactionId"],
                "index": int(item["outpoint"]["index"]),
            },
            "utxoEntry": {
                "amount": int(u["amount"]),
                "scriptPublicKey": {
                    "version": int(spk.get("version", 0)),
                    "script": spk.get("script", spk.get("scriptPublicKey")),
                },
                "blockDaaScore": int(u["blockDaaScore"]),
                "isCoinbase": bool(u["isCoinbase"]),
                "covenantId": u.get("covenantId"),
            },
        }
        if normalized["utxoEntry"]["covenantId"] is not None:
            abort("This simple builder does not support covenant UTXOs")
        if script_key(normalized["utxoEntry"]["scriptPublicKey"]) != source_script:
            abort("API returned a UTXO with the wrong locking script")
        key = outpoint_key(normalized["outpoint"])
        if key in known:
            abort("Duplicate UTXO in API response")
        known[key] = normalized
        entries.append(kaspa.UtxoEntryReference.from_dict(normalized))

    balance = sum(x["utxoEntry"]["amount"] for x in known.values())
    print(f"Available balance: {balance / SOMPI:.8f} KAS")
    if balance < send:
        abort("Insufficient available balance")

    print("Building transaction...")
    result = kaspa.create_transactions(
        entries=entries,
        change_address=change,
        network_id="mainnet",
        outputs=[{"address": destination, "amount": send}],
        priority_fee=0,
    )
    pending = result["transactions"]
    if len(pending) != 1:
        abort("Expected exactly one transaction; multi-transaction batches unsupported")
    p = pending[0]
    tx = p.transaction.to_dict()

    selected = []
    seen = set()
    input_total = 0
    for inp in tx["inputs"]:
        key = outpoint_key(inp["previousOutpoint"])
        if key in seen or key not in known:
            abort("Unknown or duplicate transaction input")
        seen.add(key)
        original = known[key]
        expected = original["utxoEntry"]
        embedded = inp.get("utxo")
        if embedded is None:
            inp["utxo"] = original  # SDK signing context for offline laptop
        else:
            data = embedded.get("utxoEntry", embedded)
            if int(data["amount"]) != expected["amount"]:
                abort("Input UTXO amount mismatch")
            if script_key(data["scriptPublicKey"]) != script_key(expected["scriptPublicKey"]):
                abort("Input UTXO script mismatch")
            if outpoint_key(embedded.get("outpoint", inp["previousOutpoint"])) != key:
                abort("Embedded input outpoint mismatch")
        selected.append(original)
        input_total += expected["amount"]
    if not selected:
        abort("Transaction has no inputs")

    destination_script = expected_script(destination)
    payment_count = 0
    change_count = 0
    change_amount = 0
    output_total = 0
    for out in tx["outputs"]:
        amount = int(out["value"])
        output_total += amount
        key = script_key(out["scriptPublicKey"])
        if key == destination_script:
            if amount != send:
                abort("Wrong payment amount")
            payment_count += 1
        elif key == source_script:
            change_amount += amount
            change_count += 1
        else:
            abort("Unknown transaction output")
    if payment_count != 1 or change_count != 1:
        abort("Expected exactly one payment and one change output")

    fee = input_total - output_total
    if not (0 <= fee <= max_fee):
        abort(f"Fee of {fee / SOMPI:.8f} KAS exceeds your maximum")
    if int(p.fee_amount) != fee:
        abort("Generator fee disagrees with calculated fee")

    # Confirm expected SDK JSON serialization works on this system.
    restored = kaspa.Transaction.from_dict(tx)
    if restored.to_dict()["outputs"] != tx["outputs"]:
        abort("SDK JSON round-trip changed transaction outputs")

    package = {
        "format": "kaspa-airgap-v1",
        "network": "mainnet",
        "source": source,
        "destination": destination,
        "change": change,
        "send_sompi": send,
        "max_fee_sompi": max_fee,
        "fee_sompi": fee,
        "utxos": selected,
        "transaction": tx,
    }

    print("\n=== BUILT, NOT SIGNED ===")
    print(f"Sending: {send / SOMPI:.8f} KAS to {destination}")
    print(f"Change:  {change_amount / SOMPI:.8f} KAS back to {source}")
    print(f"Fee:     {fee / SOMPI:.8f} KAS (max {max_fee / SOMPI:.8f})")
    print(f"File:    {OUTPUT_FILE}")
    if OUTPUT_FILE.exists():
        print("\nWARNING: An older unsigned.json exists at that location.")
        if input("Type REPLACE to overwrite it: ").strip() != "REPLACE":
            abort("Kept the previous unsigned.json")

    OUTPUT_FILE.write_text(json.dumps(package, indent=2), encoding="utf-8")
    print("\nSUCCESS: Saved unsigned.json. No signatures or network broadcasts.")
    print("IMPORTANT: Your old Linux signer and broadcaster were pinned to the 69.69 KAS test.")
    print("They must be adapted and validated for this new transaction before use.")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        abort(f"Could not build transaction: {type(exc).__name__}: {exc}")
