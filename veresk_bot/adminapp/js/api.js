/** API-клиент админки Veresk */
const AdminAPI = (() => {
  const TOKEN_KEY = "veresk_admin_token";

  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  function setToken(t) {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  }

  async function request(path, options = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      options.headers || {}
    );
    const token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    const res = await fetch(path, { ...options, headers });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = {};
    }
    if (res.status === 401) {
      setToken("");
      const err = new Error("unauthorized");
      err.status = 401;
      err.data = data;
      throw err;
    }
    if (!res.ok) {
      const err = new Error(data.error || "request_failed");
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  return {
    getToken,
    setToken,
    login: (username, password) =>
      request("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      }),
    logout: () => request("/api/admin/logout", { method: "POST" }),
    me: () => request("/api/admin/me"),
    stats: () => request("/api/admin/stats"),
    sync: () => request("/api/admin/sync", { method: "POST" }),
    clients: (params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request("/api/admin/clients" + (q ? "?" + q : ""));
    },
    client: (id) => request("/api/admin/clients/" + id),
    events: (days = 14) => request("/api/admin/events/upcoming?days=" + days),
    setEventAuto: (id, auto_send) =>
      request("/api/admin/events/" + id, {
        method: "PATCH",
        body: JSON.stringify({ auto_send }),
      }),
    campaigns: () => request("/api/admin/campaigns"),
    campaign: (id) => request("/api/admin/campaigns/" + id),
    createCampaign: (body) =>
      request("/api/admin/campaigns", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    patchCampaign: (id, body) =>
      request("/api/admin/campaigns/" + id, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    recipients: (id, params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request(
        "/api/admin/campaigns/" + id + "/recipients" + (q ? "?" + q : "")
      );
    },
    personal: (body) =>
      request("/api/admin/personal", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    accounts: () => request("/api/admin/accounts"),
    tgSettings: () => request("/api/admin/accounts/telegram/settings"),
    tgSaveSettings: (api_id, api_hash) =>
      request("/api/admin/accounts/telegram/settings", {
        method: "POST",
        body: JSON.stringify({ api_id, api_hash }),
      }),
    maxSettings: () => request("/api/admin/accounts/max/settings"),
    maxSaveSettings: (token) =>
      request("/api/admin/accounts/max/settings", {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    maxClearSettings: () =>
      request("/api/admin/accounts/max/settings", {
        method: "POST",
        body: JSON.stringify({ clear: true }),
      }),
    tgStart: (phone) =>
      request("/api/admin/accounts/telegram/start", {
        method: "POST",
        body: JSON.stringify({ phone }),
      }),
    tgConfirm: (phone, code, password) =>
      request("/api/admin/accounts/telegram/confirm", {
        method: "POST",
        body: JSON.stringify({ phone, code, password }),
      }),
    segments: () => request("/api/admin/segments"),
  };
})();
