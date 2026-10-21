const $ = (id) => document.getElementById(id);
const states = {
  pending: "待审核",
  confirmed: "已确认",
  rejected: "已拒绝",
  superseded: "已被重新分组",
  running: "运行中",
  complete: "完成",
  failed: "失败，自动重试",
  interrupted: "上次运行中断",
};
let lastScan = null;
let allEvents = [],
  eventLimit = 30,
  previewAssets = [],
  photoLimit = 60;
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
async function api(path, options = {}) {
  const response = await fetch("/api/v1" + path, {
    ...options,
    headers: { "Content-Type": "application/json" },
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  return data;
}
function notify(message) {
  $("notice").textContent = message;
}
function button(label, action) {
  const b = node("button", label);
  b.type = "button";
  b.onclick = async () => {
    b.disabled = true;
    try {
      await action();
    } catch (e) {
      notify(e.message);
    } finally {
      b.disabled = false;
    }
  };
  return b;
}
function time(value) {
  return value ? new Date(value).toLocaleString() : "尚未完成";
}
function options(id, rows, valueKey) {
  const select = $(id),
    current = select.value;
  select.replaceChildren(node("option", "不限"));
  select.firstChild.value = "";
  for (const row of rows) {
    const option = node("option", row.name);
    option.value = row[valueKey];
    select.append(option);
  }
  select.value = current;
}
async function loadTasks() {
  const status = await api("/tasks");
  $("status").textContent = `已索引 ${status.assets} 张照片`;
  $("tasks").replaceChildren();
  for (const task of status.tasks) {
    const card = node("div", undefined, "card");
    card.append(
      node(
        "strong",
        `${task.name === "scan" ? "照片扫描" : "相册同步"} · ${states[task.state] || task.state}`,
      ),
      node("p", `开始：${time(task.started_at)}`),
      node("p", `完成：${time(task.finished_at)}`),
    );
    if (task.error) card.append(node("p", task.error));
    if (task.result?.configured === false)
      card.append(node("p", "等待填写 Immich API key"));
    if (task.result?.mapping) {
      const m = task.result.mapping;
      card.append(
        node("p", `匹配 ${m.matched} / ${m.local_assets}，待匹配 ${m.missing}`),
      );
    }
    $("tasks").append(card);
  }
  if (!status.tasks.length) $("tasks").textContent = "等待后台 worker 首次运行";
  const scan = status.tasks.find((t) => t.name === "scan");
  if (scan?.state === "complete" && scan.finished_at !== lastScan) {
    lastScan = scan.finished_at;
    await loadEvents();
  }
}
function renderEvents() {
  const items = allEvents.filter(
    (e) => e.review_state === $("event-state").value,
  );
  $("events").replaceChildren();
  for (const e of items.slice(0, eventLimit)) {
    const card = node("div", undefined, "card");
    const name = node("input");
    name.value = e.name;
    name.maxLength = 200;
    name.setAttribute("aria-label", "事件名称");
    card.append(
      name,
      node(
        "p",
        `${e.asset_count} 张 · ${e.starts_at?.slice(0, 10) || "日期未定"}`,
      ),
    );
    const actions = node("div", undefined, "actions");
    actions.append(
      button("预览", async () => {
        previewAssets = await api("/assets?event=" + e.id);
        photoLimit = 60;
        $("preview-section").hidden = false;
        $("preview-title").textContent = name.value;
        renderPhotos();
        $("preview-section").scrollIntoView({ behavior: "smooth" });
      }),
      button("保存并确认", () => reviewEvent(e.id, "confirmed", name.value)),
      button(e.review_state === "rejected" ? "恢复待审核" : "拒绝", () =>
        reviewEvent(
          e.id,
          e.review_state === "rejected" ? "pending" : "rejected",
          name.value,
        ),
      ),
    );
    card.append(actions);
    $("events").append(card);
  }
  if (!items.length) $("events").textContent = "暂无事件";
  $("more-events").hidden = items.length <= eventLimit;
}
async function reviewEvent(id, state, name) {
  await api("/events/" + id, {
    method: "PATCH",
    body: JSON.stringify({ state, name }),
  });
  await loadEvents();
  notify("审核已保存，重扫后仍会保留。");
}
async function loadEvents() {
  allEvents = await api("/events");
  renderEvents();
  options(
    "event-filter",
    allEvents.filter((e) => e.review_state === "confirmed"),
    "id",
  );
}
function renderPhotos() {
  $("photos").replaceChildren();
  for (const asset of previewAssets.slice(0, photoLimit)) {
    const figure = node("figure");
    const img = node("img");
    img.loading = "lazy";
    img.src = `/api/v1/assets/${asset.id}/thumbnail`;
    img.alt = asset.path.split("/").pop();
    figure.append(img, node("figcaption", img.alt));
    $("photos").append(figure);
  }
  $("more-photos").hidden = previewAssets.length <= photoLimit;
}
async function loadCatalog() {
  const [people, tags, assignments] = await Promise.all([
    api("/people"),
    api("/tags"),
    api("/tag-assignments"),
  ]);
  $("people").textContent =
    "人物：" + (people.map((p) => p.name).join("、") || "暂无");
  $("tags").textContent =
    "标签：" + (tags.map((t) => t.name).join("、") || "暂无");
  options("person-filter", people, "name");
  options("tag-filter", tags, "name");
  $("assignments").replaceChildren();
  const pending = assignments.filter((a) => a.review_state === "pending");
  for (const a of pending.slice(0, 30)) {
    const card = node("div", undefined, "card");
    const image = node("img");
    image.src = `/api/v1/assets/${a.asset_id}/thumbnail`;
    image.loading = "lazy";
    image.width = 160;
    image.alt = a.path.split("/").pop();
    card.append(image, node("p", `${a.name} · ${image.alt}`));
    for (const state of ["confirmed", "rejected"])
      card.append(
        button(states[state], async () => {
          await api(`/assets/${a.asset_id}/tags/${a.tag_id}`, {
            method: "PATCH",
            body: JSON.stringify({ state }),
          });
          await loadCatalog();
        }),
      );
    $("assignments").append(card);
  }
  if (!pending.length) $("assignments").textContent = "暂无待审核标签";
}
async function loadAlbums() {
  const albums = await api("/smart-albums");
  $("albums").replaceChildren();
  for (const a of albums) {
    const card = node("div", undefined, "card");
    card.append(
      node("strong", a.name),
      node("p", `最近完整同步：${time(a.materialized_at)}`),
      node("p", JSON.stringify(a.query)),
    );
    $("albums").append(card);
  }
}
$("event-state").onchange = () => {
  eventLimit = 30;
  renderEvents();
};
$("more-events").onclick = () => {
  eventLimit += 30;
  renderEvents();
};
$("more-photos").onclick = () => {
  photoLimit += 60;
  renderPhotos();
};
$("album-form").onsubmit = async (event) => {
  event.preventDefault();
  const b = event.target.querySelector("button");
  b.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(event.target));
    const name = values.name.trim();
    delete values.name;
    const query = Object.fromEntries(
      Object.entries(values).filter(([, v]) => v !== ""),
    );
    if (query.event) query.event = Number(query.event);
    await api("/smart-albums", {
      method: "POST",
      body: JSON.stringify({ name, query }),
    });
    notify("相册已创建，后台将自动同步。");
    await loadAlbums();
  } catch (e) {
    notify(e.message);
  } finally {
    b.disabled = false;
  }
};
Promise.all([loadTasks(), loadEvents(), loadCatalog(), loadAlbums()]).catch(
  (e) => notify(e.message),
);
setInterval(
  () =>
    Promise.all([loadTasks(), loadAlbums()]).catch((e) => notify(e.message)),
  15000,
);
