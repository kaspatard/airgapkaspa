"""Offline Kaspa signing demo, compatible with build_fixed.py (kaspa SDK 2.1.0).

Runs on the air-gapped Linux machine. Requires kaspa installed locally.
Only handles ordinary single-address, one-recipient, one-change transactions.
The offline fee check uses input values supplied by the online computer;
these values are not independently verified on this air-gapped laptop.

Educational code, not independently audited. Test with small funds first.
"""

import getpass
import json
from pathlib import Path

import kaspa

SOMPI = 100_000_000
HERE = Path(__file__).resolve().parent
UNSIGNED = HERE / "unsigned.json"
SIGNED = HERE / "signed.json"


def stop(reason):
    raise SystemExit("REFUSED: " + reason)


def outpoint(ref):
    return (str(ref["transactionId"]).lower(), int(ref["index"]))


def locking_script(spk):
    return (int(spk["version"]), str(spk["script"]).lower())


def script_for(address):
    obj = kaspa.pay_to_address_script(kaspa.Address(address))
    return (int(obj.version), str(obj.script).lower())


def utxo_entry(u):
    return u.get("utxoEntry", u)



if SIGNED.exists():
    stop("signed.json already exists. Archive it away from the SD card before signing again")
if not UNSIGNED.is_file():
    stop("unsigned.json not found alongside this signing script")

pkg = json.loads(UNSIGNED.read_text(encoding="utf-8"))
if pkg.get("format") != "kaspa-airgap-v1" or pkg.get("network") != "mainnet":
    stop("Unknown transaction package format/network")
source = pkg["source"]
destination = pkg["destination"]
if pkg["change"] != source or source == destination:
    stop("Expected distinct recipient and change returning to the source")
if not source.startswith("kaspa:") or not destination.startswith("kaspa:"):
    stop("Not Kaspa mainnet addresses")

source_script = script_for(source)
recipient_script = script_for(destination)
send = int(pkg["send_sompi"])
max_fee = int(pkg["max_fee_sompi"])
if send <= 0 or max_fee <= 0:
    stop("Invalid send amount or fee ceiling")

utxos = pkg.get("utxos")
raw = pkg.get("transaction")
if not isinstance(utxos, list) or not utxos or not isinstance(raw, dict):
    stop("Missing UTXOs or transaction")
inputs = raw.get("inputs", [])
outputs = raw.get("outputs", [])
if len(inputs) != len(utxos) or len(outputs) != 2:
    stop("Expected one input record per UTXO and exactly two outputs")

known = {}
for u in utxos:
    ref = outpoint(u["outpoint"])
    entry = utxo_entry(u)
    if ref in known:
        stop("Duplicate UTXO")
    if u.get("address") != source:
        stop("Wrong input address")
    if locking_script(entry["scriptPublicKey"]) != source_script:
        stop("UTXO locking script does not match source")
    if int(entry.get("amount", -1)) <= 0:
        stop("Invalid input amount")
    if entry.get("covenantId") is not None or entry.get("isCoinbase"):
        stop("Covenant and coinbase UTXOs not supported by this demo")
    known[ref] = u

selected = set()
input_total = 0
for inp in inputs:
    ref = outpoint(inp["previousOutpoint"])
    if ref not in known or ref in selected:
        stop("Unknown or duplicate transaction input")
    selected.add(ref)
    if inp.get("signatureScript") not in ("", None):
        stop("Input was already signed")
    if int(inp.get("sigOpCount", -1)) != 1:
        stop("Unexpected signature-op count")
    u = known[ref]
    expected = utxo_entry(u)
    embedded = inp.get("utxo")
    if embedded is not None:
        e = utxo_entry(embedded)
        if int(e["amount"]) != int(expected["amount"]):
            stop("Embedded input amount differs from UTXO list")
        if locking_script(e["scriptPublicKey"]) != source_script:
            stop("Embedded script mismatch")
        if outpoint(embedded.get("outpoint", inp["previousOutpoint"])) != ref:
            stop("Embedded outpoint mismatch")
    inp["utxo"] = u  # signing context required by SDK, not a network output
    input_total += int(expected["amount"])
if len(selected) != len(known):
    stop("Unused UTXO metadata")

if int(raw.get("gas", 0)) != 0 or int(raw.get("lockTime", 0)) != 0:
    stop("Unexpected transaction gas or lock time")
if str(raw.get("subnetworkId", "")).lower() != "0" * 40:
    stop("Unexpected transaction subnetwork")
if raw.get("payload") not in (None, "", []):
    stop("Unexpected payload")

send_amounts = []
change_amounts = []
for o in outputs:
    amount = int(o["value"])
    if amount <= 0:
        stop("Invalid output amount")
    lock = locking_script(o["scriptPublicKey"])
    if lock == recipient_script:
        send_amounts.append(amount)
    elif lock == source_script:
        change_amounts.append(amount)
    else:
        stop("Unknown output destination")
if send_amounts != [send] or len(change_amounts) != 1:
    stop("Incorrect payment/change layout")
change = change_amounts[0]
fee = input_total - send - change
if not (0 <= fee <= max_fee) or fee != int(pkg.get("fee_sompi", -1)):
    stop("Fee mismatch or fee exceeds maximum")

# UTXO amounts in unsigned.json come from the online computer. This review
# checks internal consistency, but does NOT independently prove the on-chain fee.
print("\n=== OFFLINE TRANSACTION REVIEW ===")
print("Source:      ", source)
print("Recipient:   ", destination)
print(f"Send:         {send / SOMPI:.8f} KAS")
print(f"Change:       {change / SOMPI:.8f} KAS to source")
print(f"Fee (from supplied UTXO values): {fee / SOMPI:.8f} KAS (limit {max_fee / SOMPI:.8f})")
print(f"Selected inputs (from supplied UTXO values): {input_total / SOMPI:.8f} KAS")
print("WARNING: Offline fee verification trusts the UTXO values supplied by Windows.")
print("Compare the recipient address with your intended destination from a trusted source.")
if input("Type APPROVE to authorize this specific transaction: ").strip() != "APPROVE":
    stop("Not approved")

# Reconstruct and ensure SDK kept the signing context before handling secrets.
tx = kaspa.Transaction.from_dict(raw)
if len(tx.inputs) != len(inputs) or any(i.utxo is None for i in tx.inputs):
    stop("Kaspa SDK lost the input signing context")

visible = input("Show seed/passphrase while typing? Unsafe; hidden by default [y/N]: ").strip().lower() == "y"
if visible:
    print("WARNING: recovery words and passphrase will be visible on screen and in scrollback.")
    phrase = input("12-word recovery phrase: ").strip()
    passphrase = input("BIP39 passphrase (Enter if none): ")
else:
    phrase = getpass.getpass("12-word recovery phrase (hidden): ").strip()
    passphrase = getpass.getpass("BIP39 passphrase (hidden; Enter if none): ")
path = input("Derivation path [m/44'/111111'/0'/0/0]: ").strip() or "m/44'/111111'/0'/0/0"
try:
    seed = kaspa.Mnemonic(phrase).to_seed(passphrase)
    key = kaspa.XPrv(seed).derive_path(path).to_private_key()
finally:
    del phrase, passphrase

if key.to_address("mainnet").to_string() != source:
    stop("Key derived from recovery phrase does not control source address. Nothing signed")

signed_tx = kaspa.sign_transaction(tx, [key], verify_sig=True)
signed_data = signed_tx.to_dict()
if signed_data["outputs"] != raw["outputs"]:
    stop("Outputs changed during signing")
if len(signed_data["inputs"]) != len(inputs):
    stop("Input count changed during signing")
for before, after in zip(inputs, signed_data["inputs"]):
    if outpoint(before["previousOutpoint"]) != outpoint(after["previousOutpoint"]):
        stop("Input outpoint changed during signing")
    if not after.get("signatureScript"):
        stop("Signature missing")

result = {
    "format": "kaspa-airgap-signed-v1",
    "network": "mainnet",
    "source": source,
    "destination": destination,
    "send_sompi": send,
    "change_sompi": change,
    "fee_sompi": fee,
    "max_fee_sompi": max_fee,
    "input_total_sompi": input_total,
    "transaction": signed_data,
}
SIGNED.write_text(json.dumps(result, indent=2), encoding="utf-8")
print("\nSIGNED FILE CREATED:", SIGNED)
print("No broadcast attempted. Never copy your recovery phrase to Windows.")
