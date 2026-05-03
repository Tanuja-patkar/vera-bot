"""
Generates submission.jsonl — 30 lines, one per test pair.
Uses the Anthropic API directly (no running server needed).
Run: python3 generate_submission.py
"""
import json, re, asyncio, httpx
from pathlib import Path

EXPANDED = Path("/home/claude/vera-challenge/dataset/expanded")

SYSTEM = """You are Vera, magicpin's AI assistant for Indian merchants. Compose WhatsApp messages.

ABSOLUTE RULES:
1. Peer/colleague tone — NEVER promotional hype.
   Dentists: peer-clinical (fluoride, caries, recall). NEVER "cure"/"guaranteed".
   Salons: friendly-pro. Restaurants: warm, dish+price. Gyms: energetic. Pharmacies: safety-first.
2. Anchor on ONE verifiable fact (stat, date, citation). No generic "boost sales".
3. Single CTA at the END. Binary YES/STOP for action triggers; question for info; none for FYI.
4. WhatsApp-length. No bullet walls.
5. Hindi-English mix if merchant has 'hi' in languages.
6. No fabrication — only use data in the context.
7. NO URLs in body.
8. No preambles. No re-introduction after turn 1.

OUTPUT: JSON only, no markdown fences.
{"body":"...","cta":"open_ended|binary_yes_no|binary_confirm_cancel|multi_choice_slot|none","send_as":"vera|merchant_on_behalf","suppression_key":"...","rationale":"..."}"""


def load_json(path):
    with open(path) as f:
        return json.load(f)

def build_prompt(cat, merchant, trg, customer=None):
    mi = merchant.get("identity",{})
    mp = merchant.get("performance",{})
    ms = merchant.get("subscription",{})
    mc = merchant.get("customer_aggregate",{})
    cv = cat.get("voice",{})
    cp = cat.get("peer_stats",{})
    parts = [
        f"TRIGGER: kind={trg.get('kind')} source={trg.get('source')} urgency={trg.get('urgency',2)}",
        f"  payload={json.dumps(trg.get('payload',{}))}",
        f"  suppression_key={trg.get('suppression_key','')}",
        f"MERCHANT: {mi.get('name')} | {mi.get('city')},{mi.get('locality')} | lang={mi.get('languages')}",
        f"  sub={ms.get('status')}/{ms.get('plan')}/{ms.get('days_remaining')}d",
        f"  perf30d: views={mp.get('views')} calls={mp.get('calls')} ctr={mp.get('ctr')} Δ7d_views={mp.get('delta_7d',{}).get('views_pct')}",
        f"  offers={[o['title']+'('+o['status']+')' for o in merchant.get('offers',[])]}",
        f"  customers: ytd={mc.get('total_unique_ytd')} lapsed={mc.get('lapsed_180d_plus')} ret6m={mc.get('retention_6mo_pct')}",
        f"  signals={merchant.get('signals',[])}",
        f"  recent_conv={json.dumps(merchant.get('conversation_history',[])[-3:])}",
        f"CATEGORY: {cat.get('slug')} voice={cv.get('tone')} taboos={cv.get('vocab_taboo',cv.get('taboos',[]))}",
        f"  peer={json.dumps(cp)}",
        f"  offers={[o.get('title') for o in cat.get('offer_catalog',[])[:4]]}",
        f"  digest={json.dumps(cat.get('digest',[])[:3])}",
        f"  seasonal={json.dumps(cat.get('seasonal_beats',[]))}",
        f"  trends={json.dumps(cat.get('trend_signals',[])[:2])}",
    ]
    if customer:
        ci = customer.get("identity",{})
        cr = customer.get("relationship",{})
        parts.append(f"CUSTOMER: {ci.get('name')} lang={ci.get('language_pref')} state={customer.get('state')}")
        parts.append(f"  last_visit={cr.get('last_visit')} visits={cr.get('visits_total')} services={cr.get('services_received',[])} pref_slots={customer.get('preferences',{}).get('preferred_slots')}")
        parts.append("  send_as MUST be merchant_on_behalf")
    parts.append("\nCompose the Vera message. JSON only.")
    return "\n".join(parts)


async def call_llm(prompt):
    payload = {
        "model":"claude-sonnet-4-20250514",
        "max_tokens":1000,
        "system":SYSTEM,
        "messages":[{"role":"user","content":prompt}],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    text = "".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
    text = re.sub(r"```json\s*","",text); text = re.sub(r"```\s*","",text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m: return json.loads(m.group())
    raise ValueError(f"No JSON: {text[:200]}")


async def main():
    pairs = load_json(EXPANDED/"test_pairs.json")["pairs"]
    lines = []
    for pair in pairs:
        tid = pair["test_id"]
        trg_id = pair["trigger_id"]
        mid    = pair["merchant_id"]
        cid    = pair.get("customer_id")
        print(f"  {tid}: {mid} / {trg_id}", end="", flush=True)

        # load data
        trg_path = EXPANDED/"triggers"/f"{trg_id}.json"
        m_path   = EXPANDED/"merchants"/f"{mid}.json"

        if not trg_path.exists() or not m_path.exists():
            print(f" ⚠ missing file, skipping")
            continue

        trg      = load_json(trg_path)
        merchant = load_json(m_path)
        cat_slug = merchant.get("category_slug","")
        cat_path = EXPANDED/"categories"/f"{cat_slug}.json"
        cat      = load_json(cat_path) if cat_path.exists() else {}
        customer = None
        if cid:
            c_path = EXPANDED/"customers"/f"{cid}.json"
            if c_path.exists(): customer = load_json(c_path)

        try:
            prompt = build_prompt(cat, merchant, trg, customer)
            result = await call_llm(prompt)
            line = {
                "test_id": tid,
                "body": result.get("body",""),
                "cta": result.get("cta","open_ended"),
                "send_as": result.get("send_as","vera"),
                "suppression_key": result.get("suppression_key", trg.get("suppression_key","")),
                "rationale": result.get("rationale",""),
            }
            print(f" ✓")
        except Exception as e:
            print(f" ✗ {e}")
            line = {"test_id":tid,"body":"","cta":"open_ended","send_as":"vera","suppression_key":"","rationale":f"error: {e}"}

        lines.append(line)

    out = Path("/home/claude/vera-bot/submission.jsonl")
    with open(out,"w") as f:
        for l in lines:
            f.write(json.dumps(l, ensure_ascii=False)+"\n")
    print(f"\nWrote {len(lines)} lines to {out}")

if __name__=="__main__":
    asyncio.run(main())
