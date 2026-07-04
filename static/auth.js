/** Shared OIDC session helpers for the guide UI. */

function redirectToLogin() {
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/auth/login?next=${next}`;
}

async function authFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    headers,
    ...options,
  });
  if (response.status === 401) {
    redirectToLogin();
    throw new Error("Not authenticated");
  }
  return response;
}

async function initAuthBar(containerId = "auth-bar") {
  const container = document.getElementById(containerId);
  if (!container) return null;

  try {
    const response = await fetch("/auth/me", { credentials: "same-origin" });
    if (response.status === 401) {
      redirectToLogin();
      return null;
    }
    const data = await response.json();
    if (data.auth_disabled) {
      container.hidden = true;
      return data;
    }
    if (!data.authenticated) {
      redirectToLogin();
      return null;
    }
    container.hidden = false;
    const user = data.user || {};
    const label = user.name || user.email || user.preferred_username || "Signed in";
    container.innerHTML = `<span class="auth-user">${escapeAuthHtml(label)}</span> · <a href="/auth/logout">Log out</a>`;
    return data;
  } catch (err) {
    container.textContent = "Auth check failed";
    return null;
  }
}

function escapeAuthHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
