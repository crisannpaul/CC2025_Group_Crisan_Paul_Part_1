// =============================================
// CONFIG (PROD)
// =============================================
const API_BASE_URL =
  "https://cc-backend-paul-bbdedhg6c0dyg9eb.westeurope-01.azurewebsites.net";

const COGNITO_DOMAIN =
  "https://eu-central-1fgjfnia5z.auth.eu-central-1.amazoncognito.com";

const COGNITO_CLIENT_ID = "826c2fnsp719oaqrs5gttb20m";

const REDIRECT_URI = "https://kind-dune-0fa1d2103.3.azurestaticapps.net";

const SCOPES = ["openid", "email", "profile"];

// =============================================
// UI helpers
// =============================================
document.getElementById("api-base").textContent = API_BASE_URL;

const qs = (sel) => document.querySelector(sel);

const loginBtn = qs("#loginBtn");
const logoutBtn = qs("#logoutBtn");
const authStatus = qs("#authStatus");
const authInfo = qs("#authInfo");

// =============================================
// PKCE helpers
// =============================================
function base64urlEncode(bytes) {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function sha256(str) {
  const data = new TextEncoder().encode(str);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return new Uint8Array(hash);
}

function randomString(length = 64) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64urlEncode(bytes);
}

function parseJwt(token) {
  const [, payload] = token.split(".");
  const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
  return JSON.parse(json);
}

// =============================================
// Token storage
// =============================================
function setTokens(tokens) {
  localStorage.setItem("id_token", tokens.id_token);
  localStorage.setItem("access_token", tokens.access_token);
  if (tokens.refresh_token) localStorage.setItem("refresh_token", tokens.refresh_token);
}

function clearTokens() {
  localStorage.removeItem("id_token");
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
}

function getIdToken() {
  return localStorage.getItem("id_token");
}

// =============================================
// Auth UI
// =============================================
function setUIAuthed(isAuthed) {
  loginBtn.style.display = isAuthed ? "none" : "inline-block";
  logoutBtn.style.display = isAuthed ? "inline-block" : "none";
}

function renderAuthInfo() {
  const idToken = getIdToken();
  if (!idToken) {
    authStatus.textContent = "Not logged in.";
    authInfo.textContent = "";
    setUIAuthed(false);
    return;
  }

  const claims = parseJwt(idToken);
  const groups = claims["cognito:groups"] || [];
  const role = groups.includes("admin") ? "admin" : "user";
  const deviceId = claims["custom:device_id"] || null;

  authStatus.textContent = "Logged in ✅";
  authInfo.textContent = JSON.stringify(
    {
      email: claims.email,
      username: claims["cognito:username"] || claims.email || claims.sub,
      role,
      device_id: deviceId,
      exp: claims.exp,
    },
    null,
    2
  );

  setUIAuthed(true);
}

// =============================================
// Cognito Auth Code + PKCE
// =============================================
loginBtn.onclick = async () => {
  const verifier = randomString(64);
  const challenge = base64urlEncode(await sha256(verifier));
  sessionStorage.setItem("pkce_verifier", verifier);

  const params = new URLSearchParams({
    response_type: "code",
    client_id: COGNITO_CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: SCOPES.join(" "),
    code_challenge_method: "S256",
    code_challenge: challenge,
  });

  window.location.href = `${COGNITO_DOMAIN}/login?${params.toString()}`;
};

logoutBtn.onclick = () => {
  clearTokens();
  renderAuthInfo();

  const params = new URLSearchParams({
    client_id: COGNITO_CLIENT_ID,
    logout_uri: REDIRECT_URI,
  });
  window.location.href = `${COGNITO_DOMAIN}/logout?${params.toString()}`;
};

async function exchangeCodeForTokens(code) {
  const verifier = sessionStorage.getItem("pkce_verifier");
  if (!verifier) throw new Error("Missing PKCE verifier (sessionStorage).");

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: COGNITO_CLIENT_ID,
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: verifier,
  });

  const resp = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(`Token exchange failed: ${resp.status} ${txt}`);
  }
  return resp.json();
}

// =============================================
// API helper (Bearer JWT) — USE ID TOKEN
// =============================================
async function fetchJSON(url) {
  const headers = {};
  const idToken = getIdToken();
  if (idToken) headers["Authorization"] = `Bearer ${idToken}`;

  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// =============================================
// UI loaders for new endpoints
// =============================================
async function loadLatest() {
  try {
    const latest = await fetchJSON(`${API_BASE_URL}/api/data/latest`);
    qs("#latest").textContent = JSON.stringify(latest, null, 2);
  } catch (e) {
    qs("#latest").textContent = `Failed to load latest: ${e.message}`;
  }
}

async function loadTable() {
  const tbody = qs("#data-body");
  tbody.innerHTML = "";

  try {
    const rows = await fetchJSON(`${API_BASE_URL}/api/data`);
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${r.device_id}</td>
        <td class="mono">${r.timestamp}</td>
        <td>${Number(r.kwh).toFixed(3)}</td>
        <td>${r.location ?? ""}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (e) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="4">Failed to load data: ${e.message}</td>`;
    tbody.appendChild(tr);
  }
}

// =============================================
// Init: handle redirect back from Cognito
// =============================================
(async function init() {
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  const err = url.searchParams.get("error");

  if (err) {
    authStatus.textContent = `Auth error: ${err}`;
    authInfo.textContent = url.searchParams.get("error_description") || "";
    setUIAuthed(false);
    return;
  }

  if (code) {
    try {
      const tokens = await exchangeCodeForTokens(code);
      setTokens(tokens);

      url.searchParams.delete("code");
      url.searchParams.delete("state");
      window.history.replaceState({}, document.title, url.toString());
    } catch (e) {
      authStatus.textContent = "Token exchange failed";
      authInfo.textContent = String(e);
      clearTokens();
    }
  }

  renderAuthInfo();
  loadLatest();
  loadTable();
})();
