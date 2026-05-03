"""
Vera Bot — magicpin AI Challenge Submission
FastAPI server implementing all 5 required endpoints.
"""

import os, time, re, json, uuid, logging
from datetime import datetime, timezone
from typing import Any, Optional
import urllib.request, urllib.error

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera")

app = FastAPI(title="Vera Bot")
START = time.time()

# ── State ─────────────────────────────────────────────────────────────────────
contexts: dict[tuple[str,str], dict] = {}          # (scope,id) -> {version, payload}
conversations: dict[str, dict] = {}                # conv_id -> state
fired_keys: set[str] = set()                       # suppression keys fired this session


# ── Utilities ─────────────────────────────────────────────────────────────────
def utcnow(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def get_ctx(scope,cid): r=contexts.get((scope,cid)); return r["payload"] if r else None
def get_merchant(mid): return get_ctx("merchant",mid)
def get_category(slug): return get_ctx("category",slug)
def get_customer(cid): return get_ctx("customer",cid)
def get_trigger(tid): return get_ctx("trigger",tid)
def wants_hindi(m): return "hi" in m.get("identity",{}).get("languages",["en"])

def is_auto_reply(msg:str)->bool:
    patterns = [
        r"thank\s*you\s*for\s*contact",r"we\s*will\s*get\s*back",
        r"automated\s*(assistant|message|reply)",r"main\s*ek\s*automated",
        r"i\s*am\s*(an?\s*)?automated",r"currently\s*(unavailable|away)",
        r"team\s*tak\s*pahuncha\s*d",r"team\s*ko\s*forward",
        r"bahut.bahut\s*shukriya.*team",r"jaankari.*pahuncha",
        r"out\s*of\s*(office|hours)",r"business\s*hours",
    ]
    low=msg.lower()
    return any(re.search(p,low) for p in patterns)

def detect_intent(msg:str)->str:
    low=msg.lower()
    if any(re.search(p,low) for p in [
        r"\byes\b",r"\bhaan\b",r"\bha[an]\b",r"\bji\b",r"\bsure\b",
        r"\bgo\s*ahead\b",r"\bdo\s*it\b",r"\blet'?s?\s*do\b",
        r"\bproceed\b",r"\bconfirm\b",r"\bsend\s*(me|it)\b",
        r"\bplease\s*(do|send|update|proceed)"]):
        return "accept"
    if any(re.search(p,low) for p in [
        r"\bno\b",r"\bnahi\b",r"\bnah\b",r"\bnot\s*interested\b",
        r"\bstop\b",r"\bunsubscribe\b",r"\bdon'?t\s*(want|need)\b"]):
        return "stop"
    if any(re.search(p,low) for p in [
        r"\bjoin\b.*magicpin",r"\bjudrna\b",r"\bsign\s*up\b",r"\bregister\b"]):
        return "join"
    return "neutral"


# ── Claude API ────────────────────────────────────────────────────────────────
def call_claude(system:str, user:str, max_tokens:int=600)->str:
    payload = json.dumps({
        "model":"claude-sonnet-4-20250514",
        "max_tokens":max_tokens,
        "temperature":0,
        "system":system,
        "messages":[{"role":"user","content":user}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={"Content-Type":"application/json","anthropic-version":"2023-06-01"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req,timeout=25) as resp:
            data=json.loads(resp.read())
            return data["content"][0]["text"].strip()
    except Exception as e:
        log.error(f"Claude error: {e}"); return ""

def parse_json_response(raw:str)->dict:
    try:
        clean=re.sub(r"```(?:json)?|```","",raw).strip()
        m=re.search(r"\{.*\}",clean,re.DOTALL)
        if m: return json.loads(m.group())
    except: pass
    return {}


# ── Prompt builders ───────────────────────────────────────────────────────────
SYSTEM_COMPOSE = """You are Vera — magicpin's WhatsApp merchant AI assistant.
Compose ONE WhatsApp message using the 4-context inputs provided.

RULES:
• Voice: peer/colleague tone. Match category (clinical for dentists, energetic for gyms).
• Language: hi-en code-mix when merchant languages include "hi". English otherwise.
• Anchor on a CONCRETE fact from context (number, date, peer stat, research source). Never invent.
• Service+price format: "Haircut @ ₹99" NOT "flat X% off".
• ONE CTA at the END. Binary YES/STOP for action triggers. Open-ended question for info/research. None for urgency ≤ 1.
• No long preamble. No "I hope you are well". Get to value in sentence 1.
• Do NOT re-introduce yourself if conversation_history shows prior contact.
• Max 4 sentences. WhatsApp-friendly. 1-2 emoji max (relevant).
• Do NOT fabricate. No invented citations, offers, or competitor names.
• Use compulsion levers: specificity, loss aversion, social proof, curiosity, effort-externalization.

Return ONLY valid JSON — no markdown:
{"body":"<message>","cta":"yes_stop"|"open_ended"|"none","rationale":"<1-2 sentences>"}"""

SYSTEM_REPLY = """You are Vera in an ongoing WhatsApp conversation with a merchant.
Decide the next move given the conversation and merchant's latest reply.

RULES:
• AUTO-REPLY detected (turn 1): send one gentle "can I speak to owner?" nudge.
• AUTO-REPLY detected (turn 2+): action=wait 14400s or end.
• intent=stop / hostile: action=end with warm farewell.
• intent=join / "let's do it": NO more qualifying — go straight to action step immediately.
• intent=accept: fulfill what was offered, add logical next step.
• Off-topic (GST etc): politely decline and redirect.
• Never repeat the previous Vera message verbatim.
• After 4+ unanswered Vera messages: end.

Return ONLY valid JSON — no markdown:
{"action":"send"|"wait"|"end","body":"<msg or null>","cta":"yes_stop"|"open_ended"|"none","wait_seconds":<int or null>,"rationale":"<1-2 sentences>"}"""


def compose_context_str(cat,merchant,trg,customer=None)->str:
    mi=merchant.get("identity",{}); mp=merchant.get("performance",{})
    ms=merchant.get("subscription",{}); mc=merchant.get("customer_aggregate",{})
    cv=cat.get("voice",{}); cp=cat.get("peer_stats",{})
    digest=cat.get("digest",[])
    # Resolve top_item_id
    top_item_id=trg.get("payload",{}).get("top_item_id")
    resolved_item=next((d for d in digest if d.get("id")==top_item_id),None) if top_item_id else None

    lines=[
        f"TRIGGER: kind={trg.get('kind')} scope={trg.get('scope')} source={trg.get('source')} urgency={trg.get('urgency',2)}",
        f"  payload={json.dumps(trg.get('payload',{}))}",
        resolved_item and f"  RESOLVED_DIGEST_ITEM={json.dumps(resolved_item)}",
        f"\nMERCHANT: {mi.get('name')} | {mi.get('city')},{mi.get('locality')} | lang={mi.get('languages')} | verified={mi.get('verified')}",
        f"  subscription={ms.get('plan')} {ms.get('status')} {ms.get('days_remaining')}d remaining",
        f"  perf30d: views={mp.get('views')} calls={mp.get('calls')} ctr={mp.get('ctr')} (peer_avg_ctr={cp.get('avg_ctr')}) directions={mp.get('directions')}",
        f"  7d_delta: {mp.get('delta_7d',{})}",
        f"  offers={[o['title']+'('+o['status']+')' for o in merchant.get('offers',[])]}",
        f"  customers: ytd={mc.get('total_unique_ytd')} lapsed={mc.get('lapsed_180d_plus')} retention6m={mc.get('retention_6mo_pct')}",
        f"  signals={merchant.get('signals',[])}",
        f"  recent_conv={json.dumps(merchant.get('conversation_history',[])[-3:])}",
        f"\nCATEGORY: {cat.get('slug')} | voice_tone={cv.get('tone')} | taboos={cv.get('taboos',cv.get('vocab_taboo',[]))}",
        f"  peer_stats={json.dumps(cp)}",
        f"  offer_catalog={[o.get('title') for o in cat.get('offer_catalog',[])[:5]]}",
        f"  digest_titles={[d.get('title') for d in digest[:4]]}",
        f"  seasonal={json.dumps(cat.get('seasonal_beats',[]))}",
        f"  trends={json.dumps(cat.get('trend_signals',[])[:2])}",
    ]
    if customer:
        ci=customer.get("identity",{}); cr=customer.get("relationship",{})
        lines+=[
            f"\nCUSTOMER: {ci.get('name')} | lang={ci.get('language_pref')} | state={customer.get('state')}",
            f"  last_visit={cr.get('last_visit')} visits={cr.get('visits_total')} services={cr.get('services_received',[])}",
            f"  prefs={customer.get('preferences',{})}",
            "  NOTE: send_as MUST be 'merchant_on_behalf' — message is FROM merchant TO customer",
        ]
    return "\n".join(l for l in lines if l)


def compose_message(cat,merchant,trg,customer=None)->dict:
    send_as="merchant_on_behalf" if (customer and trg.get("scope")=="customer") else "vera"
    lang="hi-en code-mix" if wants_hindi(merchant) else "English"
    user=f"Language: {lang}\nSend as: {send_as}\n\n{compose_context_str(cat,merchant,trg,customer)}\n\nCompose now. JSON only."
    raw=call_claude(SYSTEM_COMPOSE,user)
    result=parse_json_response(raw)
    if not result.get("body"):
        name=merchant.get("identity",{}).get("name","")
        result={"body":f"Hi {name}, checking in — any updates for your profile?","cta":"open_ended","rationale":"fallback"}
    result["send_as"]=send_as
    result["suppression_key"]=trg.get("suppression_key",f"{trg.get('kind','')}:{merchant.get('merchant_id','')}")
    return result


def compose_reply(conv,msg,merchant,cat=None,customer=None)->dict:
    arc=conv.get("auto_reply_streak",0)
    if is_auto_reply(msg):
        arc+=1; conv["auto_reply_streak"]=arc
        if arc>=2:
            return {"action":"end","body":None,"cta":"none","wait_seconds":None,"rationale":"2+ auto-replies detected — exiting gracefully"}
    intent=detect_intent(msg)
    if intent=="stop":
        name=merchant.get("identity",{}).get("owner_first_name") or merchant.get("identity",{}).get("name","")
        farewell=f"Samajh gayi, koi baat nahi {name}! Jab bhi zaroorat ho main yahan hoon 🙂" if wants_hindi(merchant) else f"Understood {name}, no problem! Reach out whenever you need 🙂"
        return {"action":"end","body":farewell,"cta":"none","wait_seconds":None,"rationale":"Merchant declined — graceful exit"}
    turns=conv.get("turns",[])
    lang="hi-en" if wants_hindi(merchant) else "English"
    mi=merchant.get("identity",{})
    user=(
        f"CONV_HISTORY={json.dumps(turns[-6:])}\n"
        f"MERCHANT_REPLY: \"{msg}\"\n"
        f"detected_intent={intent} | is_auto_reply={is_auto_reply(msg)} | auto_reply_count={arc}\n"
        f"merchant={mi.get('name')} | city={mi.get('city')} | lang={lang}\n"
        +(f"category={cat.get('slug')} voice={cat.get('voice',{}).get('tone')}\n" if cat else "")
        +(f"customer={customer.get('identity',{}).get('name')} state={customer.get('state')}\n" if customer else "")
        +"Decide next move. JSON only."
    )
    raw=call_claude(SYSTEM_REPLY,user,max_tokens=400)
    result=parse_json_response(raw)
    if not result.get("action"):
        result={"action":"send","body":"Got it! Proceeding right away.","cta":"open_ended","wait_seconds":None,"rationale":"fallback"}
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/healthz")
async def healthz():
    counts={"category":0,"merchant":0,"customer":0,"trigger":0}
    for (scope,_) in contexts:
        if scope in counts: counts[scope]+=1
    return {"status":"ok","uptime_seconds":int(time.time()-START),"contexts_loaded":counts}

@app.get("/v1/metadata")
async def metadata():
    return {"team_name":"Vera++","team_members":["Participant"],
            "model":"claude-sonnet-4-20250514",
            "approach":"4-context LLM composer with trigger routing, auto-reply detection, intent-transition handling",
            "contact_email":"participant@example.com","version":"1.0.0","submitted_at":utcnow()}

class CtxBody(BaseModel):
    scope:str; context_id:str; version:int; payload:dict[str,Any]; delivered_at:str

@app.post("/v1/context")
async def push_context(body:CtxBody):
    if body.scope not in {"category","merchant","customer","trigger"}:
        return JSONResponse(400,{"accepted":False,"reason":"invalid_scope"})
    key=(body.scope,body.context_id); cur=contexts.get(key)
    if cur and cur["version"]>=body.version:
        return JSONResponse(409,{"accepted":False,"reason":"stale_version","current_version":cur["version"]})
    contexts[key]={"version":body.version,"payload":body.payload}
    log.info(f"ctx stored: {body.scope}/{body.context_id} v{body.version}")
    return {"accepted":True,"ack_id":f"ack_{body.context_id}_v{body.version}","stored_at":utcnow()}

class TickBody(BaseModel):
    now:str; available_triggers:list[str]=[]

@app.post("/v1/tick")
async def tick(body:TickBody):
    actions=[]
    for trg_id in body.available_triggers:
        trg=get_trigger(trg_id)
        if not trg: continue
        sup=trg.get("suppression_key","")
        if sup and sup in fired_keys: continue
        # expiry check
        try:
            exp=trg.get("expires_at")
            if exp and datetime.now(timezone.utc)>datetime.fromisoformat(exp.replace("Z","+00:00")): continue
        except: pass
        mid=trg.get("merchant_id")
        if not mid: continue
        merchant=get_merchant(mid)
        if not merchant: continue
        cat=get_category(merchant.get("category_slug",""))
        if not cat: continue
        cid=trg.get("customer_id"); customer=get_customer(cid) if cid else None
        conv_id=f"conv_{mid}_{trg_id}"
        if conv_id in conversations and conversations[conv_id].get("state")!="ended": continue
        try:
            c=compose_message(cat,merchant,trg,customer)
        except Exception as e:
            log.error(f"compose error {trg_id}: {e}"); continue
        body_text=c.get("body","")
        if not body_text: continue
        if sup: fired_keys.add(sup)
        conversations[conv_id]={"merchant_id":mid,"customer_id":cid,"trigger_id":trg_id,
                                "turns":[{"from":"vera","body":body_text,"ts":utcnow()}],
                                "state":"active","auto_reply_streak":0}
        actions.append({
            "conversation_id":conv_id,"merchant_id":mid,"customer_id":cid,
            "send_as":c.get("send_as","vera"),"trigger_id":trg_id,
            "template_name":f"vera_{trg.get('kind','generic')}_v1",
            "template_params":[merchant.get("identity",{}).get("name",""),trg.get("kind",""),body_text[:60]],
            "body":body_text,"cta":c.get("cta","open_ended"),
            "suppression_key":sup,"rationale":c.get("rationale",""),
        })
        log.info(f"tick action: {mid} / {trg_id}")
        if len(actions)>=20: break
    return {"actions":actions}

class ReplyBody(BaseModel):
    conversation_id:str; merchant_id:Optional[str]=None; customer_id:Optional[str]=None
    from_role:str; message:str; received_at:str; turn_number:int

@app.post("/v1/reply")
async def reply(body:ReplyBody):
    conv=conversations.get(body.conversation_id) or {
        "merchant_id":body.merchant_id,"customer_id":body.customer_id,
        "turns":[],"state":"active","auto_reply_streak":0}
    conversations[body.conversation_id]=conv
    if conv.get("state")=="ended":
        return {"action":"end","body":None,"cta":"none","wait_seconds":None,"rationale":"already ended"}
    conv["turns"].append({"from":body.from_role,"body":body.message,"ts":body.received_at})
    mid=body.merchant_id or conv.get("merchant_id")
    merchant=get_merchant(mid) if mid else {"identity":{"name":"Merchant","languages":["en"]}}
    cat=get_category((merchant or {}).get("category_slug",""))
    cid=body.customer_id or conv.get("customer_id")
    customer=get_customer(cid) if cid else None
    try:
        result=compose_reply(conv,body.message,merchant or {},cat,customer)
    except Exception as e:
        log.error(f"reply error: {e}"); result={"action":"send","body":"Got it, one moment!","cta":"open_ended","wait_seconds":None,"rationale":"fallback"}
    if result.get("action")=="end": conv["state"]="ended"
    elif result.get("action")=="send" and result.get("body"):
        conv["turns"].append({"from":"vera","body":result["body"],"ts":utcnow()})
    return result

@app.post("/v1/teardown")
async def teardown():
    contexts.clear(); conversations.clear(); fired_keys.clear()
    return {"status":"cleared"}

if __name__=="__main__":
    import uvicorn
    uvicorn.run("bot:app",host="0.0.0.0",port=int(os.environ.get("PORT",8080)),reload=False)
