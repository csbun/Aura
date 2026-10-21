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
let lastScan = null,
  lastRecognition = null;
let catalogPeople = [],
  peopleLimit = 30,
  personOffset = 0,
  tagOffset = 0;
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
    const displayState =
      task.name === "face_jobs" &&
      task.state === "complete" &&
      task.result?.phase !== "done"
        ? "running"
        : task.state;
    const card = node("div", undefined, "card");
    card.append(
      node(
        "strong",
        `${{ scan: "照片扫描", sync: "相册同步", recognition: "人物与标签同步", face_jobs: "Immich 人脸任务" }[task.name] || task.name} · ${states[displayState] || displayState}`,
      ),
      node("p", `开始：${time(task.started_at)}`),
      node("p", `完成：${time(task.finished_at)}`),
    );
    if (task.error) card.append(node("p", task.error));
    if (task.name === "face_jobs" && task.result)
      card.append(
        node(
          "p",
          {
            waiting_for_library: "等待照片映射",
            detection: "正在检测人脸",
            recognition: "正在聚合人物",
            done: "人脸任务已完成",
          }[task.result.phase] || task.result.phase,
        ),
      );
    if (task.name === "recognition" && task.result)
      card.append(
        node(
          "p",
          `已导入 ${task.result.completed} / ${task.result.total} 张，失败 ${task.result.failed} 张`,
        ),
      );
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
  const recognition = status.tasks.find((t) => t.name === "recognition");
  const catalogVersion = `${scan?.finished_at || ""}/${recognition?.finished_at || ""}`;
  if (
    catalogVersion !== lastRecognition &&
    !$("catalog").contains(document.activeElement)
  ) {
    lastRecognition = catalogVersion;
    await loadCatalog();
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
  const [people, tags] = await Promise.all([api("/people"), api("/tags")]);
  catalogPeople = people;
  renderPeople();
  $("tags").textContent = `${tags.length} 个标签，选择已确认标签可创建相册。`;
  options(
    "person-filter",
    people.filter((p) => p.confirmed),
    "id",
  );
  options("review-person-filter", people, "id");
  options(
    "tag-filter",
    tags.filter((t) => t.asset_count > 0),
    "id",
  );
  await Promise.all([loadPersonAssignments(), loadTagAssignments()]);
}
function renderPeople() {
  $("people").replaceChildren();
  for (const p of catalogPeople.slice(0, peopleLimit)) {
    const card = node("div", undefined, "card"),
      name = node("input");
    name.value = p.name;
    name.maxLength = 200;
    name.setAttribute("aria-label", "人物名称");
    card.append(
      name,
      node("p", `${p.asset_count} 张照片`),
      button("保存名字", async () => {
        await api(`/people/${p.id}`, {
          method: "PATCH",
          body: JSON.stringify({ name: name.value }),
        });
        await loadCatalog();
        notify("人物名称已保存。");
      }),
      button("查看照片", async () => {
        $("review-person-filter").value = p.id;
        $("person-review-state").value = p.confirmed ? "confirmed" : "pending";
        personOffset = 0;
        await loadPersonAssignments();
      }),
    );
    $("people").append(card);
  }
  if (!catalogPeople.length)
    $("people").textContent = "等待 Immich 人脸识别和自动导入";
  $("more-people").hidden = catalogPeople.length <= peopleLimit;
}
function assignmentCard(a) {
  const card = node("div", undefined, "card"),
    image = node("img");
  image.src = `/api/v1/assets/${a.asset_id}/thumbnail`;
  image.loading = "lazy";
  image.width = 160;
  image.alt = a.path.split("/").pop();
  card.append(image, node("p", `${a.name} · ${image.alt}`));
  card.append(
    node(
      "p",
      `来源：${a.source === "aura.metadata" ? "基础标签规则" : a.source === "manual" ? "人工" : "Immich"} · ${a.reviewed ? "人工决定" : "自动导入"}`,
    ),
  );
  return card;
}
async function loadPersonAssignments() {
  const person = $("review-person-filter").value;
  const rows = await api(
    `/person-assignments?state=${$("person-review-state").value}&limit=30&offset=${personOffset}${person ? "&person_id=" + person : ""}`,
  );
  $("person-assignments").replaceChildren();
  for (const a of rows) {
    const card = assignmentCard(a);
    for (const state of ["confirmed", "rejected"])
      card.append(
        button(states[state], async () => {
          await api(`/assets/${a.asset_id}/people/${a.person_id}`, {
            method: "PATCH",
            body: JSON.stringify({ state }),
          });
          await loadCatalog();
        }),
      );
    const select = node("select");
    select.setAttribute("aria-label", "正确的人物");
    const placeholder = node("option", "选择正确人物");
    placeholder.value = "";
    select.append(placeholder);
    for (const p of catalogPeople.filter((p) => p.id !== a.person_id)) {
      const option = node("option", p.name);
      option.value = p.id;
      select.append(option);
    }
    card.append(
      select,
      button("纠正人物", async () => {
        if (!select.value) throw new Error("请选择正确人物");
        await api(`/assets/${a.asset_id}/people/${a.person_id}/correct`, {
          method: "POST",
          body: JSON.stringify({ person_id: Number(select.value) }),
        });
        await loadCatalog();
      }),
    );
    $("person-assignments").append(card);
  }
  if (!rows.length) $("person-assignments").textContent = "暂无记录";
  $("previous-people").disabled = personOffset === 0;
  $("next-people").disabled = rows.length < 30;
}
async function loadTagAssignments() {
  const rows = await api(
    `/tag-assignments?state=${$("tag-review-state").value}&limit=30&offset=${tagOffset}`,
  );
  $("assignments").replaceChildren();
  for (const a of rows) {
    const card = assignmentCard(a);
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
  if (!rows.length) $("assignments").textContent = "暂无记录";
  $("previous-tags").disabled = tagOffset === 0;
  $("next-tags").disabled = rows.length < 30;
}
function catalogAction(id, action) {
  $(id).onclick = () =>
    Promise.resolve()
      .then(action)
      .catch((e) => notify(e.message));
}
catalogAction("more-people", () => {
  peopleLimit += 30;
  renderPeople();
});
catalogAction("previous-people", () => {
  personOffset = Math.max(0, personOffset - 30);
  return loadPersonAssignments();
});
catalogAction("next-people", () => {
  personOffset += 30;
  return loadPersonAssignments();
});
catalogAction("previous-tags", () => {
  tagOffset = Math.max(0, tagOffset - 30);
  return loadTagAssignments();
});
catalogAction("next-tags", () => {
  tagOffset += 30;
  return loadTagAssignments();
});
for (const id of ["person-review-state", "review-person-filter"])
  $(id).onchange = () => {
    personOffset = 0;
    loadPersonAssignments().catch((e) => notify(e.message));
  };
$("tag-review-state").onchange = () => {
  tagOffset = 0;
  loadTagAssignments().catch((e) => notify(e.message));
};
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
    for (const key of ["event", "person_id", "tag_id"])
      if (query[key]) query[key] = Number(query[key]);
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
