"""The business's magic-link page (PRD OP2): hours, closures and leave, prices and FAQ from
any browser, no install. The token in the URL is the only credential: it expires, can be
revoked, and only its hash is stored.

Hours and closures apply once confirmed on the page; services, prices and FAQ go to the
founder's approval queue. A link made for one staff member can only set that person's leave.
"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import booking, config_changes, events, notifications
from app.database import get_db
from app.models import AdminLink, ConfigVersion, Practice, Staff
from app.schemas import Service

router = APIRouter(prefix="/manage", tags=["manage"])
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


class HoursIn(BaseModel):
    hours: dict


class ClosureBody(BaseModel):
    date_from: date
    date_to: date
    staff_id: str | None = None
    reason: str | None = Field(default=None, max_length=200)


class ServicesIn(BaseModel):
    services: list[Service] = Field(min_length=1)


class KnowledgeIn(BaseModel):
    knowledge_base: dict[str, str]


async def _link(db: AsyncSession, token: str) -> tuple[AdminLink, Practice, str]:
    found = await config_changes.resolve_link(db, token)
    if found is None:
        raise HTTPException(status_code=404, detail="expired")
    link, practice = found
    person = await db.get(Staff, link.staff_id) if link.staff_id else None
    return link, practice, person.name if person else "link"


def _invalid(e: ValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail="; ".join(err["msg"] for err in e.errors()))


async def _done(db: AsyncSession, practice: Practice) -> None:
    await db.commit()
    notifications.kick()
    events.publish(f"practice:{practice.id}")


@router.get("/{token}", response_class=HTMLResponse)
async def page(token: str, db: AsyncSession = Depends(get_db)):
    found = await config_changes.resolve_link(db, token)
    if found is None:
        return HTMLResponse(EXPIRED, status_code=404, headers=NO_STORE)
    await db.commit()
    _, practice = found
    greek = practice.language == "el"
    cfg = json.dumps({"lang": "el" if greek else "en", "name": practice.name}).replace("</", "<\\/")
    return HTMLResponse(PAGE.replace("__LANG__", "el" if greek else "en").replace("__CONFIG__", cfg),
                        headers=NO_STORE)


@router.get("/{token}/state")
async def state(token: str, db: AsyncSession = Depends(get_db)):
    link, practice, _ = await _link(db, token)
    staff = await booking.staff_of(db, practice.id)
    closures = []
    for c in sorted(booking.rules_for(practice)["closures"] or [], key=lambda c: c["from"]):
        if c["to"] < datetime.now(ZoneInfo(practice.timezone)).date().isoformat():
            continue
        closures.append({**c, "to_rebook": len(await config_changes.to_rebook(db, practice, c))})
    pending = (await db.execute(
        select(ConfigVersion).where(ConfigVersion.practice_id == practice.id, ConfigVersion.status == "pending")
        .order_by(ConfigVersion.created_at)
    )).scalars().all()
    await db.commit()
    return {
        "name": practice.name,
        "only_staff_id": link.staff_id,
        "hours": practice.hours or {},
        "closures": closures,
        "staff": [{"id": p.id, "name": p.name} for p in staff],
        "services": practice.services or [],
        "knowledge_base": practice.knowledge_base or {},
        "pending": [{"summary": v.summary, "created_at": v.created_at.isoformat()} for v in pending],
    }


@router.put("/{token}/hours")
async def put_hours(token: str, payload: HoursIn, db: AsyncSession = Depends(get_db)):
    link, practice, author = await _link(db, token)
    if link.staff_id:
        raise HTTPException(status_code=403, detail="staff_link")
    try:
        changes = config_changes.validated(practice, {"hours": payload.hours})
    except ValidationError as e:
        raise _invalid(e)
    await config_changes.publish(db, practice, changes, source="link", author=author)
    await _done(db, practice)
    return {"status": "published"}


@router.post("/{token}/closures")
async def post_closure(token: str, payload: ClosureBody, db: AsyncSession = Depends(get_db)):
    link, practice, author = await _link(db, token)
    staff_id = link.staff_id or payload.staff_id
    try:
        closure = await config_changes.add_closure(
            db, practice, date_from=payload.date_from, date_to=payload.date_to, staff_id=staff_id,
            reason=payload.reason, source="link", author=author)
    except config_changes.ChangeError as e:
        raise HTTPException(status_code=400, detail=e.code)
    to_rebook = len(await config_changes.to_rebook(db, practice, closure))
    await _done(db, practice)
    return {"status": "published", "to_rebook": to_rebook}


@router.delete("/{token}/closures/{closure_id}")
async def delete_closure(token: str, closure_id: str, db: AsyncSession = Depends(get_db)):
    link, practice, author = await _link(db, token)
    closure = next((c for c in booking.rules_for(practice)["closures"] or [] if c["id"] == closure_id), None)
    if closure is None:
        raise HTTPException(status_code=404, detail="not_found")
    if link.staff_id and closure.get("staff_id") != link.staff_id:
        raise HTTPException(status_code=403, detail="staff_link")
    await config_changes.remove_closure(db, practice, closure_id, source="link", author=author)
    await _done(db, practice)
    return {"status": "published"}


async def _propose(db: AsyncSession, token: str, changes: dict) -> dict:
    link, practice, author = await _link(db, token)
    if link.staff_id:
        raise HTTPException(status_code=403, detail="staff_link")
    try:
        valid = config_changes.validated(practice, changes)
    except ValidationError as e:
        raise _invalid(e)
    version = await config_changes.propose(db, practice, valid, author=author)
    await _done(db, practice)
    return {"status": "pending" if version else "unchanged"}


@router.put("/{token}/services")
async def put_services(token: str, payload: ServicesIn, db: AsyncSession = Depends(get_db)):
    return await _propose(db, token, {"services": [s.model_dump(mode="json") for s in payload.services]})


@router.put("/{token}/knowledge-base")
async def put_knowledge(token: str, payload: KnowledgeIn, db: AsyncSession = Depends(get_db)):
    kb = {k.strip(): v.strip() for k, v in payload.knowledge_base.items() if k.strip() and v.strip()}
    return await _propose(db, token, {"knowledge_base": kb})


EXPIRED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Link expired</title>
<body style="font:16px system-ui;margin:0;padding:48px 16px;text-align:center;color:#333">
<p>Ο σύνδεσμος έληξε. Ζητήστε νέο.</p><p style="color:#777">This link has expired. Ask for a new one.</p></body>"""

PAGE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Settings</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--text:#1c1c1a;--muted:#6b6b66;--line:#e2e2dd;--accent:#1f5f8b;--ok:#2d7a46;--warn:#9a5b00;--danger:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--text:#ececea;--muted:#a3a39e;--line:#333330;--accent:#7cb4dc;--ok:#6cc08b;--warn:#e0a54a;--danger:#f08a82}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 24px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
h2{font-size:17px;margin:0 0 4px}
.hint{color:var(--muted);font-size:14px;margin:0 0 12px}
label{display:block;font-size:14px;color:var(--muted);margin-bottom:4px}
input,select,textarea{width:100%;font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 10px}
textarea{min-height:64px;resize:vertical}
.row{display:grid;grid-template-columns:110px 1fr;gap:8px;align-items:center;margin-bottom:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
.svc{display:grid;grid-template-columns:1fr 90px 80px;gap:8px;margin-bottom:8px}
button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:10px 14px;background:var(--accent);color:#fff;cursor:pointer;min-height:44px}
button.ghost{background:transparent;color:var(--danger);padding:6px 8px;min-height:36px}
button:disabled{opacity:.5}
ul{list-style:none;padding:0;margin:0 0 12px}
li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line)}
.warn{color:var(--warn);font-size:14px}
.pending{color:var(--warn);font-size:14px;margin-top:8px}
#toast{position:fixed;left:16px;right:16px;bottom:16px;max-width:608px;margin:0 auto;padding:12px 14px;border-radius:10px;background:var(--text);color:var(--bg);display:none}
@media (max-width:420px){.row{grid-template-columns:1fr}.svc{grid-template-columns:1fr 1fr}.svc>:first-child{grid-column:1/-1}.svc[aria-hidden]>:first-child{display:none}}
</style>
</head>
<body>
<main>
<h1 id="title"></h1>
<p class="sub" id="subtitle"></p>
<div id="app"></div>
</main>
<div id="toast" role="status"></div>
<script>
const CFG = __CONFIG__;
const EL = CFG.lang === "el";
const T = EL ? {
  subtitle: "Αλλαγές για τον ψηφιακό βοηθό",
  hours: "Ωράριο", hoursHint: "Π.χ. 09:00-14:00, 17:00-21:00. Κενό σημαίνει κλειστά.",
  days: ["Δευτέρα","Τρίτη","Τετάρτη","Πέμπτη","Παρασκευή","Σάββατο","Κυριακή"],
  closures: "Κλειστά και άδειες", closuresHint: "Ο βοηθός δεν κλείνει ραντεβού αυτές τις μέρες και το λέει στους πελάτες.",
  from: "Από", to: "Έως", who: "Ποιος", whole: "Όλη η επιχείρηση", reason: "Αιτία (προαιρετικό)",
  add: "Προσθήκη", save: "Αποθήκευση", remove: "Αφαίρεση", none: "Καμία",
  rebook: n => `${n} ραντεβού χρειάζονται αλλαγή· θα σας έρθει email.`,
  services: "Υπηρεσίες και τιμές", servicesHint: "Οι αλλαγές ελέγχονται πριν τις πει ο βοηθός.",
  name: "Όνομα", price: "Τιμή", minutes: "Λεπτά", addService: "+ Υπηρεσία",
  info: "Πληροφορίες", infoHint: "Διεύθυνση, πάρκινγκ, ασφάλειες, συχνές ερωτήσεις. Ελέγχονται πριν τις πει ο βοηθός.",
  addInfo: "+ Πληροφορία", topic: "Θέμα", answer: "Απάντηση",
  send: "Αποστολή για έλεγχο", pending: "Σε αναμονή ελέγχου:",
  confirm: "Να ισχύσει αυτή η αλλαγή;\n\n", saved: "Αποθηκεύτηκε.", sent: "Στάλθηκε για έλεγχο.",
  same: "Δεν άλλαξε τίποτα.", error: "Κάτι πήγε στραβά: ", expired: "Ο σύνδεσμος έληξε. Ζητήστε νέο.",
  closed: "κλειστά"
} : {
  subtitle: "Changes for the digital assistant",
  hours: "Opening hours", hoursHint: "E.g. 09:00-14:00, 17:00-21:00. Empty means closed.",
  days: ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],
  closures: "Closures and leave", closuresHint: "The assistant books nothing on these days and tells callers.",
  from: "From", to: "To", who: "Who", whole: "Whole business", reason: "Reason (optional)",
  add: "Add", save: "Save", remove: "Remove", none: "None",
  rebook: n => `${n} appointments need rebooking; you'll get an email.`,
  services: "Services and prices", servicesHint: "Changes are checked before the assistant says them.",
  name: "Name", price: "Price", minutes: "Min", addService: "+ Service",
  info: "Information", infoHint: "Address, parking, insurance, FAQs. Checked before the assistant says them.",
  addInfo: "+ Item", topic: "Topic", answer: "Answer",
  send: "Send for review", pending: "Waiting for review:",
  confirm: "Apply this change?\n\n", saved: "Saved.", sent: "Sent for review.",
  same: "Nothing changed.", error: "Something went wrong: ", expired: "This link has expired. Ask for a new one.",
  closed: "closed"
};
const KEYS = ["mon","tue","wed","thu","fri","sat","sun"];
const base = location.pathname.replace(/\/$/, "");
const app = document.getElementById("app");
document.getElementById("title").textContent = CFG.name;
document.getElementById("subtitle").textContent = T.subtitle;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "on") for (const [ev, f] of Object.entries(v)) n.addEventListener(ev, f);
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const k of kids) if (k) n.append(k);
  return n;
}
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = "none", 4000);
}
async function api(method, path, body) {
  const r = await fetch(base + path, {method, headers: {"Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (r.status === 404 && data.detail === "expired") { app.textContent = T.expired; throw new Error("expired"); }
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : r.status);
  return data;
}
async function run(summary, f) {
  if (!confirm(T.confirm + summary)) return;
  try { const out = await f(); await load(); return out; } catch (e) { if (e.message !== "expired") toast(T.error + e.message); }
}
const spans = s => s.split(",").map(x => x.trim()).filter(Boolean).map(x => x.split("-").map(y => y.trim()));

function hoursSection(s) {
  const inputs = {};
  const rows = KEYS.map((k, i) => {
    inputs[k] = el("input", {value: (s.hours[k] || []).map(p => p.join("-")).join(", "), placeholder: T.closed,
                             "aria-label": T.days[i]});
    return el("div", {class: "row"}, el("label", {text: T.days[i]}), inputs[k]);
  });
  const save = el("button", {text: T.save, on: {click: () => {
    const hours = {}; const lines = [];
    KEYS.forEach((k, i) => { hours[k] = spans(inputs[k].value); lines.push(`${T.days[i]}: ${inputs[k].value.trim() || T.closed}`); });
    run(lines.join("\n"), () => api("PUT", "/hours", {hours})).then(r => r && toast(T.saved));
  }}});
  return el("section", {}, el("h2", {text: T.hours}), el("p", {class: "hint", text: T.hoursHint}), ...rows, save);
}

function closuresSection(s) {
  const names = Object.fromEntries(s.staff.map(p => [p.id, p.name]));
  const list = el("ul");
  if (!s.closures.length) list.append(el("li", {text: T.none}));
  for (const c of s.closures) {
    const who = c.staff_id ? names[c.staff_id] || "?" : T.whole;
    const text = `${c.from} – ${c.to} · ${who}${c.reason ? " (" + c.reason + ")" : ""}`;
    const canRemove = !s.only_staff_id || c.staff_id === s.only_staff_id;
    list.append(el("li", {}, el("span", {}, el("div", {text}),
      c.to_rebook ? el("div", {class: "warn", text: T.rebook(c.to_rebook)}) : null),
      canRemove ? el("button", {class: "ghost", text: T.remove, on: {click: () =>
        run(`${T.remove}: ${text}`, () => api("DELETE", "/closures/" + encodeURIComponent(c.id))).then(r => r && toast(T.saved))}}) : null));
  }
  const today = new Date().toISOString().slice(0, 10);
  const from = el("input", {type: "date", min: today, value: today});
  const to = el("input", {type: "date", min: today, value: today});
  from.addEventListener("change", () => { if (to.value < from.value) to.value = from.value; });
  const who = el("select");
  if (s.only_staff_id) who.append(el("option", {value: s.only_staff_id, text: names[s.only_staff_id] || "?"}));
  else {
    who.append(el("option", {value: "", text: T.whole}));
    for (const p of s.staff) who.append(el("option", {value: p.id, text: p.name}));
  }
  const reason = el("input", {maxlength: "200"});
  const add = el("button", {text: T.add, on: {click: () => {
    if (!from.value || !to.value || to.value < from.value) return;
    const whoText = who.options[who.selectedIndex].text;
    run(`${whoText}: ${from.value} – ${to.value}`, () => api("POST", "/closures",
      {date_from: from.value, date_to: to.value, staff_id: who.value || null, reason: reason.value.trim() || null}))
      .then(r => r && toast(r.to_rebook ? T.rebook(r.to_rebook) : T.saved));
  }}});
  return el("section", {}, el("h2", {text: T.closures}), el("p", {class: "hint", text: T.closuresHint}), list,
    el("div", {class: "grid2"}, el("div", {}, el("label", {text: T.from}), from), el("div", {}, el("label", {text: T.to}), to)),
    el("div", {class: "grid2"}, el("div", {}, el("label", {text: T.who}), who), el("div", {}, el("label", {text: T.reason}), reason)),
    add);
}

function servicesSection(s) {
  const box = el("div");
  const addRow = (v = {}) => {
    const row = el("div", {class: "svc"},
      el("input", {value: v.name || "", placeholder: T.name, "aria-label": T.name}),
      el("input", {value: v.price || "", placeholder: T.price, "aria-label": T.price}),
      el("input", {value: v.duration_minutes || 30, type: "number", min: "5", max: "480", "aria-label": T.minutes}));
    row.dataset.id = v.id || "";
    box.append(row);
  };
  s.services.forEach(addRow);
  const slug = t => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9α-ω]+/g, "-")
    .replace(/[^a-z0-9\-]/g, "") || "s";
  const send = el("button", {text: T.send, on: {click: () => {
    const used = new Set();
    const services = [...box.children].map((r, i) => {
      const [n, p, d] = r.querySelectorAll("input");
      if (!n.value.trim()) return null;
      let id = r.dataset.id || slug(n.value) || "s" + i;
      while (used.has(id)) id += "-" + i;
      used.add(id);
      return {id, name: n.value.trim(), price: p.value.trim() || null, duration_minutes: parseInt(d.value, 10) || 30};
    }).filter(Boolean);
    const lines = services.map(x => `${x.name}: ${x.price || "-"}, ${x.duration_minutes}'`);
    run(lines.join("\n"), () => api("PUT", "/services", {services})).then(r => r && toast(r.status === "pending" ? T.sent : T.same));
  }}});
  const head = el("div", {class: "svc", "aria-hidden": "true"},
    el("label", {text: T.name}), el("label", {text: T.price}), el("label", {text: T.minutes}));
  return el("section", {}, el("h2", {text: T.services}), el("p", {class: "hint", text: T.servicesHint}), head, box,
    el("button", {class: "ghost", style: "color:var(--accent)", text: T.addService, on: {click: () => addRow()}}), el("div"), send);
}

function infoSection(s) {
  const box = el("div");
  const addRow = (k = "", v = "") => box.append(el("div", {style: "margin-bottom:12px"},
    el("input", {value: k, placeholder: T.topic, "aria-label": T.topic, style: "margin-bottom:4px"}),
    el("textarea", {placeholder: T.answer, "aria-label": T.answer}, document.createTextNode(v))));
  Object.entries(s.knowledge_base).forEach(([k, v]) => addRow(k, v));
  const send = el("button", {text: T.send, on: {click: () => {
    const kb = {};
    for (const r of box.children) {
      const k = r.querySelector("input").value.trim(), v = r.querySelector("textarea").value.trim();
      if (k && v) kb[k] = v;
    }
    run(Object.entries(kb).map(([k, v]) => `${k}: ${v}`).join("\n"), () => api("PUT", "/knowledge-base", {knowledge_base: kb}))
      .then(r => r && toast(r.status === "pending" ? T.sent : T.same));
  }}});
  return el("section", {}, el("h2", {text: T.info}), el("p", {class: "hint", text: T.infoHint}), box,
    el("button", {class: "ghost", style: "color:var(--accent)", text: T.addInfo, on: {click: () => addRow()}}), el("div"), send);
}

async function load() {
  let s;
  try { s = await api("GET", "/state"); } catch (e) { return; }
  const parts = [];
  if (!s.only_staff_id) parts.push(hoursSection(s));
  parts.push(closuresSection(s));
  if (!s.only_staff_id) parts.push(servicesSection(s), infoSection(s));
  if (s.pending.length) parts.push(el("section", {}, el("h2", {text: T.pending}),
    el("ul", {}, ...s.pending.map(p => el("li", {text: p.summary})))));
  app.replaceChildren(...parts);
}
load();
</script>
</body>
</html>
"""
