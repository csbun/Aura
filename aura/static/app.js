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
  no_people: "未发现人物",
};
let editingAlbum = null,
  previewSerial = 0,
  previewTimer;
let catalogTags = [];
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
      task.result?.phase === "no_people"
        ? "no_people"
        : task.name === "face_jobs" &&
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
    if (task.result?.warning) card.append(node("p", task.result.warning));
    if (task.name === "face_jobs" && task.result)
      card.append(
        node(
          "p",
          {
            waiting_for_library: "等待照片映射",
            detection: "正在检测人脸",
            recognition: "正在聚合人物",
            done: "Immich 已生成人物",
            no_people: "队列已空闲，尚未生成人物",
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
  catalogTags = tags;
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
      node(
        "p",
        a.matched_assets
          ? `当前匹配 ${a.matched_assets} 张照片`
          : "当前匹配 0 张：请调整筛选条件",
      ),
      node("p", `最近检查：${time(a.materialized_at)}`),
      node("p", describeAlbumQuery(a.query)),
      button("编辑条件", () => editAlbum(a)),
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
function readAlbumQuery() {
  const values = Object.fromEntries(new FormData($("album-form")));
  delete values.name;
  const query = Object.fromEntries(
    Object.entries(values).filter(([, v]) => v !== ""),
  );
  for (const key of ["event", "person_id", "tag_id"])
    if (query[key]) query[key] = Number(query[key]);
  return query;
}
const filterLabels = {
  person: "人物",
  person_id: "人物",
  event: "事件",
  tag: "标签",
  tag_id: "标签",
  start: "开始日期",
  end: "结束日期",
  q: "文件路径",
};
function describeAlbumQuery(query) {
  return (
    Object.entries(query)
      .map(([key, value]) => {
        const source =
          key === "person_id"
            ? catalogPeople
            : key === "event"
              ? allEvents
              : key === "tag_id"
                ? catalogTags
                : [];
        return `${filterLabels[key] || key}：${source.find((row) => row.id === value)?.name || value}`;
      })
      .join("；") || "全部照片"
  );
}
async function previewAlbum() {
  const serial = ++previewSerial;
  $("save-album").disabled = true;
  $("album-match-summary").textContent = "正在计算匹配照片…";
  try {
    const result = await api("/smart-albums/preview", {
      method: "POST",
      body: JSON.stringify({ query: readAlbumQuery() }),
    });
    if (serial !== previewSerial) return;
    const counts = Object.entries(result.condition_counts)
      .map(([key, count]) => `${filterLabels[key] || key}单独匹配 ${count} 张`)
      .join("；");
    $("album-match-summary").textContent = result.matched_assets
      ? `同时满足所有条件：${result.matched_assets} 张，已关联 Immich：${result.mapped_assets} 张。`
      : `没有同时满足所有条件的照片，请调整筛选。${counts}`;
    $("album-preview-photos").replaceChildren();
    for (const asset of result.assets) {
      const image = node("img");
      image.loading = "lazy";
      image.src = `/api/v1/assets/${asset.id}/thumbnail`;
      image.alt = "匹配照片预览";
      $("album-preview-photos").append(image);
    }
    $("save-album").disabled = result.matched_assets === 0;
    return result;
  } catch (e) {
    if (serial === previewSerial)
      $("album-match-summary").textContent = e.message;
    throw e;
  }
}
async function editAlbum(album) {
  const query = { ...album.query };
  for (const [key, rows] of [
    ["person", catalogPeople],
    ["tag", catalogTags],
  ]) {
    if (query[key] && !query[key + "_id"]) {
      const found = rows.filter((row) => row.name === query[key]);
      if (found.length !== 1)
        throw new Error("旧规则名称无法唯一对应，请先确认人物或标签名称");
      query[key + "_id"] = found[0].id;
    }
  }
  editingAlbum = album.id;
  const form = $("album-form");
  form.reset();
  form.elements.name.value = album.name;
  form.elements.name.readOnly = true;
  for (const key of ["person_id", "tag_id", "event", "start", "end", "q"]) {
    const field = form.elements[key];
    const value = query[key] ?? "";
    if (
      field.tagName === "SELECT" &&
      value !== "" &&
      !Array.from(field.options).some((o) => o.value === String(value))
    ) {
      const option = node("option", describeAlbumQuery({ [key]: value }));
      option.value = value;
      field.append(option);
    }
    field.value = value;
  }
  $("album-form-title").textContent = "编辑相册条件";
  $("save-album").textContent = "保存条件，自动同步";
  $("cancel-album-edit").hidden = false;
  notify(
    "修改条件后仍使用同一个 Immich 相册。当前仅追加照片，不会移除已有照片。",
  );
  form.scrollIntoView({ behavior: "smooth" });
  await previewAlbum();
}
$("cancel-album-edit").onclick = () => {
  editingAlbum = null;
  $("album-form").reset();
  $("album-form").elements.name.readOnly = false;
  $("album-form-title").textContent = "创建智能相册";
  $("save-album").textContent = "创建，自动同步";
  $("cancel-album-edit").hidden = true;
  previewAlbum().catch((e) => notify(e.message));
};
$("album-form").oninput = () => {
  clearTimeout(previewTimer);
  ++previewSerial;
  $("save-album").disabled = true;
  previewTimer = setTimeout(
    () => previewAlbum().catch((e) => notify(e.message)),
    250,
  );
};
$("album-form").onsubmit = async (event) => {
  event.preventDefault();
  clearTimeout(previewTimer);
  $("save-album").disabled = true;
  try {
    const query = readAlbumQuery();
    const preview = await api("/smart-albums/preview", {
      method: "POST",
      body: JSON.stringify({ query }),
    });
    if (!preview.matched_assets)
      throw new Error("没有匹配照片，请先调整筛选条件。");
    if (editingAlbum !== null)
      await api(`/smart-albums/${editingAlbum}`, {
        method: "PATCH",
        body: JSON.stringify({ query }),
      });
    else
      await api("/smart-albums", {
        method: "POST",
        body: JSON.stringify({
          name: $("album-form").elements.name.value.trim(),
          query,
        }),
      });
    notify(
      `条件已保存，匹配 ${preview.matched_assets} 张照片，后台将自动同步。`,
    );
    await loadAlbums();
  } catch (e) {
    notify(e.message);
  } finally {
    previewAlbum().catch((e) => notify(e.message));
  }
};
previewAlbum().catch((e) => notify(e.message));
Promise.all([loadTasks(), loadEvents(), loadCatalog(), loadAlbums()]).catch(
  (e) => notify(e.message),
);
setInterval(
  () =>
    Promise.all([loadTasks(), loadAlbums()]).catch((e) => notify(e.message)),
  15000,
);
