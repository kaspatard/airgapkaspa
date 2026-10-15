"""Online broadcaster for signed.json from kaspa_offline_sign_reusable.py.

Requires kaspa Python SDK and internet. Submits an already-signed transaction.
Does not read any seed phrase or private key. Educational, not audited.
"""
import asyncio
import json
from pathlib import Path

import kaspa

SIGNED = Path(__file__).resolve().with_name("signed.json")
SOMPI = 100_000_000


def stop(reason):
    raise SystemExit("REFUSED: " + reason)


def script_key(spk):
    return (int(spk["version"]), str(spk["script"]).lower())


def script_for(address):
    obj = kaspa.pay_to_address_script(kaspa.Address(address))
    return (int(obj.version), str(obj.script).lower())


async def main():
    if not SIGNED.is_file():
        stop(f"signed.json missing alongside script at {SIGNED}")
    pkg = json.loads(SIGNED.read_text(encoding="utf-8"))
    if pkg.get("format") != "kaspa-airgap-signed-v1" or pkg.get("network") != "mainnet":
        stop("Wrong signed package format/network")
    source, recipient = pkg["source"], pkg["destination"]
    if source == recipient or not source.startswith("kaspa:") or not recipient.startswith("kaspa:"):
        stop("Unexpected source/recipient address")
    send = int(pkg["send_sompi"])
    change = int(pkg["change_sompi"])
    fee = int(pkg["fee_sompi"])
    max_fee = int(pkg["max_fee_sompi"])
    input_total = int(pkg["input_total_sompi"])
    if send <= 0 or change <= 0 or not (0 <= fee <= max_fee) or input_total != send + change + fee:
        stop("Inconsistent transaction accounting")

    data = pkg["transaction"]
    if not isinstance(data, dict) or not data.get("inputs") or len(data.get("outputs", [])) != 2:
        stop("Incorrect transaction structure")
    if any(not x.get("signatureScript") for x in data["inputs"]):
        stop("Missing input signature")
    if int(data.get("gas", 0)) != 0 or int(data.get("lockTime", 0)) != 0:
        stop("Unexpected transaction gas or locktime")
    if str(data.get("subnetworkId", "")).lower() != "0" * 40:
        stop("Unexpected subnetwork")

    recipient_script = script_for(recipient)
    source_script = script_for(source)
    send_outputs, change_outputs = [], []
    for out in data["outputs"]:
        amount = int(out["value"])
        script = script_key(out["scriptPublicKey"])
        if script == recipient_script:
            send_outputs.append(amount)
        elif script == source_script:
            change_outputs.append(amount)
        else:
            stop("Unknown output destination")
    if send_outputs != [send] or change_outputs != [change]:
        stop("Outputs do not match offline-signed summary")

    signed_tx = kaspa.Transaction.from_dict(data)
    print(f"BROADCASTING {send / SOMPI:.8f} KAS to {recipient}")
    print(f"Fee: {fee / SOMPI:.8f} KAS | Change: {change / SOMPI:.8f} KAS")
    # Check on screen before submitting; no extra wallet secrets required.
    if input("Type BROADCAST to send this transaction to mainnet: ").strip() != "BROADCAST":
        stop("Not approved")

    client = kaspa.RpcClient(resolver=kaspa.Resolver(), network_id="mainnet")
    print("Connecting to Kaspa node (15-second max)...", flush=True)
    try:
        await asyncio.wait_for(client.connect(strategy="fallback", timeout_duration=10000), timeout=15)
    except Exception as exc:
        stop(f"Could not connect: {type(exc).__name__}: {exc} (no submission attempted)")
    try:
        print("Connected. Submitting...", flush=True)
        try:
            result = await asyncio.wait_for(
                client.submit_transaction({"transaction": signed_tx, "allowOrphan": False}),
                timeout=20,
            )
        except Exception as exc:
            stop(f"SUBMISSION STATUS UNKNOWN: {type(exc).__name__}: {exc}. Check explorer before retrying.")
        print("TRANSACTION SUBMITTED")
        print("Transaction ID:", result["transactionId"])
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
