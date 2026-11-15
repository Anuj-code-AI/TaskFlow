const API = "/api/v1";
let accessToken = null;

export function setAccessToken(token) { accessToken = token; }
export function clearAccessToken() { accessToken = null; }

export async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response = await fetch(`${API}${path}`, { ...options, headers, credentials: "include" });
  if (response.status === 401 && !path.startsWith("/auth/")) {
    const refreshed = await refresh();
    if (refreshed) {
      headers.set("Authorization", `Bearer ${accessToken}`);
      response = await fetch(`${API}${path}`, { ...options, headers, credentials: "include" });
    }
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { const data = await response.json(); message = data.detail || message; } catch {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

export async function refresh() {
  try {
    const response = await fetch(`${API}/auth/refresh`, { method: "POST", credentials: "include" });
    if (!response.ok) { accessToken = null; return false; }
    const data = await response.json();
    accessToken = data.access_token;
    return true;
  } catch { accessToken = null; return false; }
}
