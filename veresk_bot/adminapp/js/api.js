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
    const raw = await res.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_) {
      data = {
        error: "bad_response",
        detail:
          "Сервер вернул не JSON (HTTP " +
          res.status +
          "). Часто это старый контейнер bot — пересоберите: docker compose build bot && docker compose up -d bot",
        status: res.status,
        raw: String(raw || "").slice(0, 200),
      };
    }
    if (res.status === 401) {
      setToken("");
      const err = new Error("unauthorized");
      err.status = 401;
      err.data = data;
      throw err;
    }
    if (!res.ok) {
      const err = new Error(data.error || data.detail || "request_failed");
      err.status = res.status;
      err.data = Object.assign({ status: res.status }, data);
      throw err;
    }
    return data;
  }

  async function requestForm(path, formData, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    const token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    // Не ставим Content-Type — boundary выставит браузер
    const res = await fetch(path, {
      ...options,
      method: options.method || "POST",
      headers,
      body: formData,
    });
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
    users: () => request("/api/admin/users"),
    user: (id) => request("/api/admin/users/" + id),
    createUser: (body) =>
      request("/api/admin/users", {
        method: "POST",
        body: JSON.stringify(body || {}),
      }),
    updateUser: (id, body) =>
      request("/api/admin/users/" + id, {
        method: "PATCH",
        body: JSON.stringify(body || {}),
      }),
    deleteUser: (id) =>
      request("/api/admin/users/" + id, { method: "DELETE" }),
    resetUserPassword: (id, body) =>
      request("/api/admin/users/" + id + "/password", {
        method: "POST",
        body: JSON.stringify(body || {}),
      }),
    generatePassword: (length = 10) =>
      request("/api/admin/users/generate-password", {
        method: "POST",
        body: JSON.stringify({ length }),
      }),
    stats: () => request("/api/admin/stats"),
    botsStatus: () => request("/api/admin/bots/status"),
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
    mailingPreview: (params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request("/api/admin/mailing/preview" + (q ? "?" + q : ""));
    },
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
    accounts: (params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request("/api/admin/accounts" + (q ? "?" + q : ""));
    },
    tgSettings: () => request("/api/admin/accounts/telegram/settings"),
    tgSaveSettings: (api_id, api_hash) =>
      request("/api/admin/accounts/telegram/settings", {
        method: "POST",
        body: JSON.stringify({ api_id, api_hash }),
      }),
    maxSettings: () => request("/api/admin/accounts/max/settings"),
    maxSaveSettings: (body) =>
      request("/api/admin/accounts/max/settings", {
        method: "POST",
        body: JSON.stringify(
          typeof body === "string" ? { token: body } : body || {}
        ),
      }),
    maxClearSettings: () =>
      request("/api/admin/accounts/max/settings", {
        method: "POST",
        body: JSON.stringify({ clear: true }),
      }),
    maxClearWebhook: () =>
      request("/api/admin/accounts/max/settings", {
        method: "POST",
        body: JSON.stringify({ clear_webhook: true }),
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
    maxUserbotStart: (phone, opts = {}) =>
      request("/api/admin/accounts/max/userbot/start", {
        method: "POST",
        body: JSON.stringify({
          phone,
          reset: opts.reset !== false,
        }),
      }),
    maxUserbotConfirm: (phone, code, password) =>
      request("/api/admin/accounts/max/userbot/confirm", {
        method: "POST",
        body: JSON.stringify({ phone, code, password }),
      }),
    tgKeepalive: () =>
      request("/api/admin/accounts/telegram/keepalive", { method: "POST" }),
    tgCheckAccount: (id) =>
      request("/api/admin/accounts/" + id + "/check", { method: "POST" }),
    tgDeleteAccount: (id) =>
      request("/api/admin/accounts/" + id, { method: "DELETE" }),
    segments: () => request("/api/admin/segments"),
    aiCompose: (body) =>
      request("/api/admin/ai/compose", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    aiChat: (body) =>
      request("/api/admin/ai/chat", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    aiSettings: () => request("/api/admin/ai/settings"),
    aiSaveSettings: (body) =>
      request("/api/admin/ai/settings", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    chatAccounts: () => request("/api/admin/chats/accounts"),
    chatDialogs: (params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request("/api/admin/chats/dialogs" + (q ? "?" + q : ""));
    },
    chatMessages: (peerId, params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request(
        "/api/admin/chats/dialogs/" +
          encodeURIComponent(peerId) +
          "/messages" +
          (q ? "?" + q : "")
      );
    },
    chatSend: (peerId, body) =>
      request("/api/admin/chats/dialogs/" + encodeURIComponent(peerId) + "/send", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    chatSendMedia: (peerId, formData) =>
      requestForm(
        "/api/admin/chats/dialogs/" + encodeURIComponent(peerId) + "/send-media",
        formData
      ),
    chatClientStatus: (peerId, accountId) => {
      const q = new URLSearchParams({ account_id: String(accountId || "") });
      return request(
        "/api/admin/chats/dialogs/" +
          encodeURIComponent(peerId) +
          "/client?" +
          q.toString()
      );
    },
    chatClientCreate: (peerId, body) =>
      request("/api/admin/chats/dialogs/" + encodeURIComponent(peerId) + "/client", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    chatCreate: (body) =>
      request("/api/admin/chats/dialogs", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    chatAvatarUrl: (peerId, accountId) => {
      const q = new URLSearchParams({
        account_id: String(accountId || ""),
        token: getToken(),
      });
      return (
        "/api/admin/chats/dialogs/" +
        encodeURIComponent(peerId) +
        "/avatar?" +
        q.toString()
      );
    },
    chatMediaUrl: (peerId, messageId, accountId, opts = {}) => {
      const q = new URLSearchParams({
        account_id: String(accountId || ""),
        token: getToken(),
      });
      if (opts.thumb) q.set("thumb", "1");
      return (
        "/api/admin/chats/dialogs/" +
        encodeURIComponent(peerId) +
        "/messages/" +
        encodeURIComponent(messageId) +
        "/media?" +
        q.toString()
      );
    },
    maxChatStatus: () => request("/api/admin/max-chats/status"),
    maxChatDialogs: (params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request("/api/admin/max-chats/dialogs" + (q ? "?" + q : ""));
    },
    maxChatMessages: (peerId, params = {}) => {
      const q = new URLSearchParams(params).toString();
      return request(
        "/api/admin/max-chats/dialogs/" +
          encodeURIComponent(peerId) +
          "/messages" +
          (q ? "?" + q : "")
      );
    },
    maxChatSend: (peerId, body) =>
      request("/api/admin/max-chats/dialogs/" + encodeURIComponent(peerId) + "/send", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    maxChatSendMedia: (peerId, formData) =>
      requestForm(
        "/api/admin/max-chats/dialogs/" + encodeURIComponent(peerId) + "/send-media",
        formData
      ),
    maxChatMediaUrl: (peerId, messageId, accountId, opts = {}) => {
      const q = new URLSearchParams({ token: getToken() });
      if (accountId) q.set("account_id", String(accountId));
      if (opts.thumb) q.set("thumb", "1");
      return (
        "/api/admin/max-chats/dialogs/" +
        encodeURIComponent(peerId) +
        "/messages/" +
        encodeURIComponent(messageId) +
        "/media?" +
        q.toString()
      );
    },
    maxChatCreate: (body) =>
      request("/api/admin/max-chats/dialogs", {
        method: "POST",
        body: JSON.stringify(body || {}),
      }),
    maxChatClientStatus: (peerId, accountId) => {
      const q = new URLSearchParams();
      if (accountId) q.set("account_id", String(accountId));
      const qs = q.toString();
      return request(
        "/api/admin/max-chats/dialogs/" +
          encodeURIComponent(peerId) +
          "/client" +
          (qs ? "?" + qs : "")
      );
    },
    maxChatClientCreate: (peerId, body) =>
      request(
        "/api/admin/max-chats/dialogs/" + encodeURIComponent(peerId) + "/client",
        {
          method: "POST",
          body: JSON.stringify(body || {}),
        }
      ),
    maxChatEvents: (onEvent) => {
      const q = new URLSearchParams({ token: getToken() });
      const es = new EventSource(
        "/api/admin/max-chats/events?" + q.toString()
      );
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (typeof onEvent === "function") onEvent(data);
        } catch (_) {
          /* ignore malformed */
        }
      };
      return es;
    },
  };
})();
