import { API_BASE } from "./config.js";

const ACCESS = "clab_access";
const REFRESH = "clab_refresh";

const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {} },
};

export const tokens = {
  get access() { return store.get(ACCESS); },
  get refresh() { return store.get(REFRESH); },
  set(access, refresh) { if (access) store.set(ACCESS, access); if (refresh) store.set(REFRESH, refresh); },
  clear() { store.set(ACCESS, null); store.set(REFRESH, null); },
  isLoggedIn() { return !!store.get(ACCESS); },
};


// Social login lands with #access=...&refresh=... in the URL fragment.
(() => {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get("access")) {
    tokens.set(p.get("access"), p.get("refresh"));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();

function errorText(data, fallback) {
  const d = data?.detail ?? data?.error ?? data?.message ?? data;
  if (!d) return fallback;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.join(" ");
  return Object.entries(d).map(([k, v]) => `${k === "non_field_errors" ? "" : k + ": "}${[].concat(v).join(" ")}`).join(" · ");
}

async function refreshAccess() {
  if (!tokens.refresh) return false;
  const res = await fetch(`${API_BASE}/api/auth/token/refresh`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh: tokens.refresh }),
  }).catch(() => null);
  if (!res?.ok) return false;
  tokens.set((await res.json()).access);
  return true;
}

export async function request(path, { method = "GET", body, raw = false } = {}, retried = false) {
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (tokens.access) headers.Authorization = `Bearer ${tokens.access}`;
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method, headers, body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new Error("Can't reach the server. Check your connection.");
  }
  if (res.status === 401 && !retried && tokens.refresh) {
    if (await refreshAccess()) return request(path, { method, body, raw }, true);
    tokens.clear();
    location.href = "/login.html";
    throw new Error("Session expired.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(errorText(data, `${res.status} ${res.statusText}`));
  }
  if (raw) return res;
  if (res.status === 204) return null;
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
}

const get = (p) => request(p);
const post = (p, body = {}) => request(p, { method: "POST", body });
const patch = (p, body) => request(p, { method: "PATCH", body });
const put = (p, body) => request(p, { method: "PUT", body });
const del = (p) => request(p, { method: "DELETE" });
const qs = (params = {}) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)).toString();
  return s ? `?${s}` : "";
};

export const api = {
  register: (b) => post("/api/auth/register", b),
  login: (email, password) => post("/api/auth/login", { email, password }),
  me: () => get("/api/auth/me"),
  changePassword: (b) => post("/api/auth/change-password", b),
  mcpKeys: () => get("/api/mcp/keys"),
  mcpCreateKey: (name) => post("/api/mcp/keys", { name }),
  mcpRevokeKey: (id) => del(`/api/mcp/keys/${id}`),

  markets: () => get("/api/markets"),
  investSummary: () => get("/api/invest/summary"),
  investCharts: () => get("/api/invest/charts"),
  investMoney: () => get("/api/invest/money"),
  deals: (refresh) => get(`/api/invest/deals${refresh ? "?refresh=true" : ""}`),
  refreshDeals: () => post("/api/invest/deals/refresh"),
  pulse: (refresh) => get(`/api/invest/pulse${refresh ? "?refresh=true" : ""}`),
  technical: (symbol) => get(`/api/invest/technical/${encodeURIComponent(symbol)}`),
  investQuote: (symbol) => get(`/api/invest/quote/${encodeURIComponent(symbol)}`),
  investTxns: () => get("/api/invest/transactions"),
  addInvestTxn: (b) => post("/api/invest/transactions", b),
  deleteInvestTxn: (id) => del(`/api/invest/transactions/${id}`),
  importInvestCsv: (fd) => post("/api/invest/transactions/import", fd),
  investTemplate: () => request("/api/invest/transactions/template", { raw: true }),
  setManualPrice: (symbol, price) => put(`/api/invest/prices/${encodeURIComponent(symbol)}`, { price }),
  clearManualPrice: (symbol) => del(`/api/invest/prices/${encodeURIComponent(symbol)}`),
  watchlist: () => get("/api/invest/watchlist"),
  addWatch: (b) => post("/api/invest/watchlist", b),
  updateWatch: (id, b) => patch(`/api/invest/watchlist/${id}`, b),
  deleteWatch: (id) => del(`/api/invest/watchlist/${id}`),
  properties: () => get("/api/invest/properties"),
  addProperty: (b) => post("/api/invest/properties", b),
  updateProperty: (id, b) => put(`/api/invest/properties/${id}`, b),
  deleteProperty: (id) => del(`/api/invest/properties/${id}`),

  onboarding: () => get("/api/onboarding"),
  onboardingId: (id_number) => put("/api/onboarding/id", { id_number }),
  onboardingStatements: () => post("/api/onboarding/statements"),
  emailSend: () => post("/api/onboarding/email/send"),
  emailVerify: (code) => post("/api/onboarding/email/verify", { code }),
  report: (start, end) => get(`/api/reports?start=${start}&end=${end}`),
  reportAI: (start, end) => get(`/api/reports/ai?start=${start}&end=${end}`),
  aiStatus: () => get("/api/ai/status"),
  aiSuggestions: (section, refresh) => get(`/api/ai/suggestions/${section}${refresh ? "?refresh=true" : ""}`),
  aiChats: () => get("/api/ai/chats"),
  aiChat: (id) => get(`/api/ai/chats/${id}`),
  aiDeleteChat: (id) => del(`/api/ai/chats/${id}`),
  aiConverse: (b) => post("/api/ai/converse", b),
  blog: () => get("/api/invest/blog"),
  dividendCalendar: () => get("/api/invest/calendar"),
  refreshCalendar: () => post("/api/invest/calendar/refresh"),
  blogRefresh: () => post("/api/invest/blog/refresh"),
  blogWatch: (b) => post("/api/invest/blog/watch", b),
  bank: () => get("/api/bank"),
  bankSync: () => post("/api/bank/sync"),
  bankCategories: () => get("/api/bank/categories"),
  bankCategorise: () => post("/api/bank/categorise"),
  setBankCategory: (id, b) => patch(`/api/bank/transactions/${id}`, b),
  bankTxns: () => get("/api/bank/transactions"),
  bankStatements: () => get("/api/bank/statements"),
  bankStatementText: (id) => get(`/api/bank/statements/${id}/text`),
  updateBankAccount: (id, b) => patch(`/api/bank/accounts/${id}`, b),
  addLiability: (b) => post("/api/bank/liabilities", b),
  updateLiability: (id, b) => put(`/api/bank/liabilities/${id}`, b),
  deleteLiability: (id) => del(`/api/bank/liabilities/${id}`),
  ee: () => get("/api/ee"),
  eeConnectPlatform: (b) => put("/api/ee/platform", b),
  eeDisconnectPlatform: () => del("/api/ee/platform"),
  eeConnectMail: (b) => put("/api/ee/mail", b),
  eeGoogleStart: () => get("/api/ee/google/start"),
  eeDisconnectMail: () => del("/api/ee/mail"),
  eeSync: () => post("/api/ee/sync"),
  eeReparse: () => post("/api/ee/reparse"),
  eeTransactions: () => get("/api/ee/transactions"),
  eeStatements: () => get("/api/ee/statements"),
  eeReadStatements: () => post("/api/ee/statements/read"),
  eeReaderStatus: () => get("/api/ee/statements/reader"),
  eeSetPdfPassword: (password) => put("/api/ee/statements/password", { password }),
  eeStatementText: (id) => get(`/api/ee/statements/${id}/text`),
  eeStatementPdf: (id, inline = false) => request(`/api/ee/statements/${id}${inline ? "?inline=true" : ""}`, { raw: true }),
  eeMails: (kind = "") => get(`/api/ee/mails${qs({ kind })}`),
  eeMail: (id) => get(`/api/ee/mails/${id}`),
  eeUpdateMail: (id, b) => patch(`/api/ee/mails/${id}`, b),
};
