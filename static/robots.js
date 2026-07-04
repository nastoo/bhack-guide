/** Robot picker — shared by home page and /robot chat. */
async function robotApi(path, options = {}) {
  const response = await authFetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function robotMetaText(robot, selectedId) {
  if (!robot) return "Robot: unknown";
  const sim = robot.simulated ? "simulated" : "live";
  return `${robot.label || selectedId} (${robot.api}, ${sim})`;
}

async function initRobotPicker(selectEl, metaEl) {
  if (!selectEl) return null;
  try {
    const data = await robotApi("/api/robots");
    selectEl.innerHTML = "";
    const robots = data.robots || {};
    for (const [id, profile] of Object.entries(robots)) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = profile.label || id;
      if (id === data.selected) opt.selected = true;
      selectEl.appendChild(opt);
    }
    if (metaEl) {
      metaEl.textContent = robotMetaText(robots[data.selected], data.selected);
    }
    selectEl.addEventListener("change", async () => {
      selectEl.disabled = true;
      try {
        const result = await robotApi("/api/robot/select", {
          method: "POST",
          body: JSON.stringify({ robot: selectEl.value }),
        });
        if (metaEl && result.robot) {
          metaEl.textContent = robotMetaText(result.robot, result.selected);
        }
      } catch (err) {
        if (metaEl) metaEl.textContent = `Error: ${err.message}`;
      } finally {
        selectEl.disabled = false;
      }
    });
    return data;
  } catch (err) {
    if (metaEl) metaEl.textContent = `Robot list failed: ${err.message}`;
    return null;
  }
}
