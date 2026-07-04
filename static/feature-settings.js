/** Feature toggles — persisted in localStorage, applied via data-feature attributes. */

const FEATURE_STORAGE_KEY = "guide_feature_settings";

const FEATURE_DEFAULTS = {
  gps_navigation: true,
  navigator: true,
  audio_test: true,
  direct_robot_chat: true,
  force_robot_route: false,
};

const FEATURE_TEXT_DEFAULTS = {
  force_robot_route_text: "",
};

function loadFeatureSettings() {
  try {
    const raw = localStorage.getItem(FEATURE_STORAGE_KEY);
    if (!raw) return { ...FEATURE_DEFAULTS, ...FEATURE_TEXT_DEFAULTS };
    const parsed = JSON.parse(raw);
    return { ...FEATURE_DEFAULTS, ...FEATURE_TEXT_DEFAULTS, ...parsed };
  } catch {
    return { ...FEATURE_DEFAULTS, ...FEATURE_TEXT_DEFAULTS };
  }
}

function saveFeatureSettings(settings) {
  const merged = { ...FEATURE_DEFAULTS, ...FEATURE_TEXT_DEFAULTS, ...settings };
  localStorage.setItem(FEATURE_STORAGE_KEY, JSON.stringify(merged));
  return merged;
}

/** When force route is on, return API fields to override robot LLM behavior. */
function getForceRouteOptions() {
  const settings = loadFeatureSettings();
  if (!settings.force_robot_route) return {};
  const routeText = String(settings.force_robot_route_text || "").trim();
  if (!routeText) return {};
  return { force_route: true, route_text: routeText };
}

function isFeatureEnabled(key) {
  return Boolean(loadFeatureSettings()[key]);
}

function applyFeatureVisibility(root = document) {
  const settings = loadFeatureSettings();
  root.querySelectorAll("[data-feature]").forEach((el) => {
    const key = el.getAttribute("data-feature");
    if (!key || !(key in FEATURE_DEFAULTS)) return;
    el.hidden = !settings[key];
  });
}

function guardFeaturePage(key, redirectTo = "/") {
  if (isFeatureEnabled(key)) return;
  window.location.replace(redirectTo);
}

function syncForceRouteFieldVisibility(form) {
  const toggle = form.querySelector('[name="force_robot_route"]');
  const field = form.querySelector("[data-force-route-field]");
  if (!toggle || !field) return;
  field.hidden = !toggle.checked;
}

function readFeatureSettingsFromForm(form) {
  const next = {};
  Object.keys(FEATURE_DEFAULTS).forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    next[key] = input ? input.checked : FEATURE_DEFAULTS[key];
  });
  Object.keys(FEATURE_TEXT_DEFAULTS).forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    next[key] = input ? input.value : FEATURE_TEXT_DEFAULTS[key];
  });
  return next;
}

function applyFeatureSettingsToForm(form, settings) {
  Object.keys(FEATURE_DEFAULTS).forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) input.checked = settings[key];
  });
  Object.keys(FEATURE_TEXT_DEFAULTS).forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) input.value = settings[key];
  });
  syncForceRouteFieldVisibility(form);
}

function initFeatureSettingsPage() {
  const form = document.getElementById("feature-settings-form");
  if (!form) return;

  applyFeatureSettingsToForm(form, loadFeatureSettings());

  form.addEventListener("change", (event) => {
    saveFeatureSettings(readFeatureSettingsFromForm(form));
    applyFeatureVisibility();
    if (event.target?.name === "force_robot_route") {
      syncForceRouteFieldVisibility(form);
    }
  });

  form.addEventListener("input", (event) => {
    if (event.target?.name === "force_robot_route_text") {
      saveFeatureSettings(readFeatureSettingsFromForm(form));
    }
  });

  const resetBtn = document.getElementById("feature-settings-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      const defaults = { ...FEATURE_DEFAULTS, ...FEATURE_TEXT_DEFAULTS };
      saveFeatureSettings(defaults);
      applyFeatureSettingsToForm(form, defaults);
      applyFeatureVisibility();
    });
  }
}
