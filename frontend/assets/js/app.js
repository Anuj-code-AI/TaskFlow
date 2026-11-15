import { request, clearAccessToken, refresh } from "./api.js";

const $ = (id) => document.getElementById(id);
const state = {
  user: null, workspaces: [], workspace: null, projects: [], project: null,
  tasks: [], members: [], role: null, eventSource: null,
};
const statuses = ["BACKLOG", "TODO", "IN_PROGRESS", "IN_REVIEW", "DONE"];
const labels = {
  BACKLOG: "Backlog", TODO: "To do", IN_PROGRESS: "In progress",
  IN_REVIEW: "In review", DONE: "Done",
};
const isAdmin = () => ["OWNER", "ADMIN"].includes(state.role);
const esc = (value = "") => String(value).replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const api = (path, options = {}) => request(path, options);
const json = (method, body, extra = {}) => ({
  method, body: body === undefined ? undefined : JSON.stringify(body), ...extra,
});

function toast(message) {
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  $("toast-region").append(node);
  setTimeout(() => node.remove(), 5500);
}

function setTitle(title) { $("page-title").textContent = title; }

function openModal(title, fields, onSave, options = {}) {
  $("modal-title").textContent = title;
  $("modal-fields").innerHTML = fields.map(f => {
    const common = `id="${f.id}" ${f.required ? "required" : ""} ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ""}`;
    if (f.type === "textarea") {
      return `<label class="field">${esc(f.label)}<textarea ${common}>${esc(f.value || "")}</textarea></label>`;
    }
    if (f.type === "select") {
      return `<label class="field">${esc(f.label)}<select ${common}>${(f.options || []).map(o =>
        `<option value="${esc(o.value)}" ${String(o.value) === String(f.value) ? "selected" : ""}>${esc(o.label)}</option>`
      ).join("")}</select></label>`;
    }
    if (f.type === "multiselect") {
      const selected = new Set(f.value || []);
      return `<label class="field">${esc(f.label)}<select ${common} multiple size="${Math.min(Math.max((f.options || []).length, 3), 7)}">${(f.options || []).map(o =>
        `<option value="${esc(o.value)}" ${selected.has(o.value) ? "selected" : ""}>${esc(o.label)}</option>`
      ).join("")}</select><small class="muted">Use Ctrl (Windows) or Command (Mac) to select multiple tasks.</small></label>`;
    }
    return `<label class="field">${esc(f.label)}<input ${common} type="${f.type || "text"}" value="${esc(f.value || "")}"></label>`;
  }).join("");

  $("modal-submit").classList.toggle("hidden", options.showSave === false);
  $("modal-submit").textContent = options.saveLabel || "Save";
  $("modal").showModal();

  $("modal-form").onsubmit = async (event) => {
    // A dialog form's Cancel button also emits submit. Never call the API for it.
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    const data = {};
    for (const f of fields) {
      const control = $(f.id);
      data[f.id] = f.type === "multiselect"
        ? Array.from(control?.selectedOptions || []).map(option => option.value)
        : (control?.value ?? "");
    }
    $("modal-submit").disabled = true;
    try {
      await onSave(data);
      $("modal").close();
    } catch (error) {
      toast(error.message || "Something went wrong.");
    } finally {
      $("modal-submit").disabled = false;
    }
  };
}

async function connectEventStream() {
  if (state.eventSource) state.eventSource.close();
  try {
    const { ticket } = await api("/notifications/stream-ticket", json("POST", {}));
    const stream = new EventSource(`/api/v1/notifications/stream?ticket=${encodeURIComponent(ticket)}`);
    state.eventSource = stream;
    stream.addEventListener("message", async (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "workspace_invitation") {
          toast(`Workspace invitation: ${data.workspace_name} (${data.role}).`);
          await updateInvitationCount();
          if ($("page-title").textContent === "Invitations") await showInvitations();
        } else {
          toast(data.message || "You have a new update.");
          await updateUnread();
        }
      } catch {
        toast("You have a new workspace update.");
      }
    });
    stream.onerror = () => {
      // Renew the short-lived stream ticket after a failed connection.
      stream.close();
      if (state.eventSource === stream) {
        setTimeout(() => connectEventStream(), 4000);
      }
    };
  } catch (error) {
    console.warn("Live event stream unavailable:", error);
  }
}

async function loadWorkspaces() {
  state.workspaces = await api("/workspaces");
  $("workspace-list").innerHTML = state.workspaces.map(w =>
    `<button class="workspace-item ${state.workspace?.id === w.id ? "active" : ""}" data-id="${esc(w.id)}">▦ ${esc(w.name)}</button>`
  ).join("");
  document.querySelectorAll(".workspace-item").forEach(btn =>
    btn.addEventListener("click", () => selectWorkspace(btn.dataset.id))
  );
}

async function loadWorkspaceMembers() {
  state.members = await api(`/workspaces/${state.workspace.id}/members`);
  state.role = state.members.find(m => m.user_id === state.user.id)?.role || null;
}

async function selectWorkspace(id) {
  state.workspace = state.workspaces.find(w => w.id === id);
  state.project = null;
  await loadWorkspaces();
  await renderWorkspace();
}

async function renderWorkspace() {
  if (!state.workspace) {
    setTitle("Your workspaces");
    $("add-project").classList.add("hidden");
    $("toolbar").innerHTML = "";
    $("content").innerHTML = `<div class="empty"><h3>Create your first workspace</h3><p class="muted">A workspace keeps projects, tasks and your team in one place.</p><button class="button button-primary" id="empty-create">Create workspace</button></div>`;
    $("empty-create").onclick = createWorkspace;
    return;
  }

  await loadWorkspaceMembers();
  setTitle(state.workspace.name);
  $("add-project").classList.toggle("hidden", !isAdmin());
  state.projects = await api(`/workspaces/${state.workspace.id}/projects`);
  $("toolbar").innerHTML = `
    <span class="muted">${state.projects.length} project${state.projects.length === 1 ? "" : "s"}</span>
    <div class="toolbar-actions">
      <button class="button button-outline" id="workspace-logs">Activity logs</button>
      <button class="button button-outline" id="members-button">Members & invitations</button>
    </div>`;
  $("workspace-logs").onclick = () => showAuditLogs();
  $("members-button").onclick = showMembers;

  $("content").innerHTML = state.projects.length
    ? `<div class="project-grid">${state.projects.map(p => `
        <article class="project-card" data-id="${esc(p.id)}">
          <span class="pill">PROJECT</span><h3>${esc(p.name)}</h3>
          <p>${esc(p.description || "No description added.")}</p>
          <small class="muted">Open project →</small>
        </article>`).join("")}</div>`
    : `<div class="empty"><h3>No projects yet</h3><p class="muted">${isAdmin() ? "Create a project to start assigning work." : "An owner or admin can create a project here."}</p>${isAdmin() ? '<button class="button button-primary" id="first-project">Create project</button>' : ""}</div>`;

  document.querySelectorAll(".project-card").forEach(card =>
    card.onclick = () => openProject(card.dataset.id)
  );
  if ($("first-project")) $("first-project").onclick = createProject;
}

async function createWorkspace() {
  openModal("Create workspace", [
    { id: "name", label: "Workspace name", required: true },
    { id: "description", label: "Description", type: "textarea" },
  ], async data => {
    const workspace = await api("/workspaces", json("POST", data));
    await loadWorkspaces();
    await selectWorkspace(workspace.id);
  });
}

async function createProject() {
  if (!isAdmin()) return toast("Only workspace owners and admins can create projects.");
  openModal("Create project", [
    { id: "name", label: "Project name", required: true },
    { id: "description", label: "Description", type: "textarea" },
  ], async data => {
    await api(`/workspaces/${state.workspace.id}/projects`, json("POST", data));
    await renderWorkspace();
  });
}

async function editProject() {
  if (!isAdmin() || !state.project) return;
  openModal("Edit project", [
    { id: "name", label: "Project name", value: state.project.name, required: true },
    { id: "description", label: "Description", type: "textarea", value: state.project.description || "" },
  ], async data => {
    state.project = await api(`/workspaces/${state.workspace.id}/projects/${state.project.id}`, json("PATCH", data));
    await renderWorkspace();
    await openProject(state.project.id);
    toast("Project details updated.");
  });
}

async function openProject(id) {
  state.project = state.projects.find(p => p.id === id);
  await loadTasks();
  setTitle(state.project.name);
  $("add-project").classList.add("hidden");
  $("toolbar").innerHTML = `
    <button class="button button-outline" id="back-workspace">← Projects</button>
    <span class="muted">${esc(state.project.description || "Project task board")}</span>
    <div class="toolbar-actions">
      <button class="button button-outline" id="project-logs">Activity logs</button>
      ${isAdmin() ? '<button class="button button-outline" id="edit-project">Edit project</button>' : ""}
      <button class="button button-primary" id="new-task">${isAdmin() ? "＋ Assign task" : "＋ Create my task"}</button>
    </div>`;
  $("back-workspace").onclick = renderWorkspace;
  $("project-logs").onclick = () => showAuditLogs(state.project.id);
  if ($("edit-project")) $("edit-project").onclick = editProject;
  if ($("new-task")) $("new-task").onclick = createTask;
  renderBoard();
}

async function loadTasks() {
  state.tasks = await api(`/workspaces/${state.workspace.id}/tasks?project_id=${state.project.id}&limit=100`);
}

function renderBoard() {
  $("content").innerHTML = `<div class="board">${statuses.map(status => {
    const tasks = state.tasks.filter(t => t.status === status);
    return `<section class="board-column">
      <div class="column-head"><span>${labels[status]}</span><span class="column-count">${tasks.length}</span></div>
      ${tasks.map(t => {
        const assignee = state.members.find(m => m.user_id === t.assignee_id);
        return `<article class="task-card" data-id="${esc(t.id)}">
          <h4>${esc(t.title)}</h4>
          <div class="task-meta"><span class="priority ${t.priority}">${t.priority}</span>
          <span>${esc(assignee?.name || "Unassigned")}</span><span>v${t.version}</span>
          ${t.due_at ? `<span>${new Date(t.due_at).toLocaleDateString()}</span>` : ""}</div>
        </article>`;
      }).join("")}
    </section>`;
  }).join("")}</div>`;
  document.querySelectorAll(".task-card").forEach(card =>
    card.onclick = () => showTask(card.dataset.id)
  );
}

async function createTask() {
  const canAssignOthers = isAdmin();
  if (!canAssignOthers && state.role !== "MEMBER") return toast("You cannot create tasks in this workspace.");
  await loadWorkspaceMembers();
  const [assignableTasks] = await Promise.all([
    api(`/workspaces/${state.workspace.id}/tasks?limit=100`),
  ]);
  const assignable = state.members.filter(m => m.role !== "OWNER" || m.user_id === state.workspace.created_by);
  const dependencyCandidates = assignableTasks.filter(t => t.status !== "DONE");
  const fields = [
    { id: "title", label: "Task title", required: true },
    { id: "description", label: "Description", type: "textarea" },
    ...(canAssignOthers ? [{ id: "priority", label: "Priority", type: "select", value: "MEDIUM",
      options: ["LOW", "MEDIUM", "HIGH", "URGENT"].map(x => ({ value: x, label: x })) }] : []),
    ...(canAssignOthers ? [{ id: "assignee_id", label: "Assign to team member", type: "select", required: true,
      options: assignable.map(m => ({ value: m.user_id, label: `${m.name} (${m.role})` })) }] : []),
    { id: "due_at", label: "Due date", type: "date" },
    ...(canAssignOthers ? [{ id: "depends_on_task_ids", label: "Depends on (complete these tasks first)", type: "multiselect",
      options: dependencyCandidates.map(t => ({ value: t.id, label: `${t.title} · ${labels[t.status]}` })) }] : []),
  ];
  openModal(canAssignOthers ? "Assign a task" : "Create my task", fields, async data => {
    const assigneeId = canAssignOthers ? data.assignee_id : state.user.id;
    if (!assigneeId) throw new Error("Select a team member before creating the task.");
    const payload = {
      title: data.title,
      description: data.description,
      priority: canAssignOthers ? data.priority : "MEDIUM",
      assignee_id: assigneeId,
      project_id: state.project.id,
      due_at: data.due_at ? new Date(`${data.due_at}T23:59:00`).toISOString() : null,
      depends_on_task_ids: data.depends_on_task_ids || [],
    };
    await api(`/workspaces/${state.workspace.id}/tasks`, json("POST", payload, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }));
    await loadTasks();
    renderBoard();
  }, { saveLabel: canAssignOthers ? "Assign task" : "Create task" });
}

async function showTask(id) {
  const task = state.tasks.find(t => t.id === id) ||
    await api(`/workspaces/${state.workspace.id}/tasks/${id}`);
  const [comments, dependencies, workspaceTasks] = await Promise.all([
    api(`/workspaces/${state.workspace.id}/tasks/${id}/comments`),
    api(`/workspaces/${state.workspace.id}/tasks/${id}/dependencies`),
    api(`/workspaces/${state.workspace.id}/tasks?limit=100`),
  ]);
  const assignedToMe = task.assignee_id === state.user.id;
  const canManageDetails = isAdmin();
  const canMoveAssignedTask = state.role === "MEMBER" && assignedToMe && !["IN_REVIEW", "DONE"].includes(task.status);
  const memberNextStatus = ({ BACKLOG: "TODO", TODO: "IN_PROGRESS", IN_PROGRESS: "IN_REVIEW" })[task.status];
  const incompleteDependencies = dependencies.filter(d => {
    const prerequisite = workspaceTasks.find(t => t.id === d.depends_on_task_id);
    return !prerequisite || prerequisite.status !== "DONE";
  });
  const dependenciesSatisfied = incompleteDependencies.length === 0;
  const dependencyIds = new Set(dependencies.map(d => d.depends_on_task_id));
  const dependencyCandidates = workspaceTasks.filter(t =>
    t.id !== task.id && !dependencyIds.has(t.id) && t.status !== "DONE"
  );
  const adminStatusOptions = dependenciesSatisfied
    ? statuses
    : statuses.filter(status => status === task.status || !["IN_PROGRESS", "IN_REVIEW", "DONE"].includes(status));
  const memberStatusOptions = canMoveAssignedTask
    ? [task.status, ...(memberNextStatus && (memberNextStatus === "TODO" || dependenciesSatisfied) ? [memberNextStatus] : [])]
    : [];
  const fields = [];
  if (canManageDetails) {
    fields.push(
      { id: "title", label: "Title", value: task.title, required: true },
      { id: "description", label: "Description", type: "textarea", value: task.description || "" },
      { id: "due_at", label: "Due date", type: "date", value: task.due_at ? new Date(task.due_at).toISOString().slice(0, 10) : "" },
      { id: "labels", label: "Labels (comma separated)", value: (task.labels || []).join(", "), placeholder: "backend, urgent" },
      { id: "priority", label: "Priority", type: "select", value: task.priority,
        options: ["LOW", "MEDIUM", "HIGH", "URGENT"].map(x => ({ value: x, label: x })) },
      { id: "status", label: "Status", type: "select", value: task.status,
        options: adminStatusOptions.map(x => ({ value: x, label: labels[x] })) },
    );
  } else if (canMoveAssignedTask) {
    fields.push({ id: "status", label: "Move your task one step forward", type: "select",
      value: task.status, options: memberStatusOptions.map(x => ({ value: x, label: labels[x] })) });
  }
  fields.push({ id: "comment", label: "Add a comment", type: "textarea", placeholder: "Write an update..." });
  if (canManageDetails) {
    fields.push({
      id: "assignee_id", label: "Assigned team member", type: "select",
      value: task.assignee_id || state.members[0]?.user_id || "",
      required: true,
      options: state.members.map(m => ({ value: m.user_id, label: `${m.name} (${m.role})` })),
    });
    fields.push({
      id: "add_dependency_id", label: "Add prerequisite task (this task waits for it)",
      type: "select", value: "",
      options: [{ value: "", label: "No new dependency" }, ...dependencyCandidates.map(t => ({
        value: t.id, label: `${t.title} · ${labels[t.status]}`,
      }))],
    });
  }

  openModal("Task details", fields, async data => {
    let current = task;
    if (canManageDetails) {
      const patch = {
        expected_version: current.version,
        title: data.title,
        description: data.description,
        due_at: data.due_at ? new Date(`${data.due_at}T23:59:00`).toISOString() : null,
        labels: data.labels.split(",").map(label => label.trim()).filter(Boolean),
        priority: data.priority,
      };
      current = await api(`/workspaces/${state.workspace.id}/tasks/${id}`, json("PATCH", patch));
      if (data.assignee_id !== undefined && data.assignee_id !== (task.assignee_id || "")) {
        current = await api(`/workspaces/${state.workspace.id}/tasks/${id}/assign`,
          json("POST", { expected_version: current.version, assignee_id: data.assignee_id }));
      }
      if (data.add_dependency_id) {
        await api(`/workspaces/${state.workspace.id}/tasks/${id}/dependencies`,
          json("POST", { depends_on_task_id: data.add_dependency_id }));
        current = { ...current, version: current.version + 1 };
      }
    }
    if ((canManageDetails || canMoveAssignedTask) && data.status && data.status !== current.status) {
      current = await api(`/workspaces/${state.workspace.id}/tasks/${id}/transition`,
        json("POST", { expected_version: current.version, status: data.status }));
    }
    if (data.comment?.trim()) {
      await api(`/workspaces/${state.workspace.id}/tasks/${id}/comments`,
        json("POST", { body: data.comment }));
    }
    if (!canManageDetails && !canMoveAssignedTask && !data.comment?.trim()) return;
    await loadTasks();
    renderBoard();
  }, { showSave: true, saveLabel: canManageDetails ? "Save changes" : "Submit" });

  const commentRows = comments.map(comment => {
    const author = state.members.find(m => m.user_id === comment.author_id);
    return `<article class="comment-row"><div><strong>${esc(author?.name || "Workspace member")}</strong> <small class="muted">${new Date(comment.created_at).toLocaleString()}</small></div><p>${esc(comment.body)}</p></article>`;
  }).join("");
  const dependencyNames = dependencies.map(d => {
    const blocker = workspaceTasks.find(t => t.id === d.depends_on_task_id);
    return `<div class="dependency-row"><span>${esc(blocker?.title || d.depends_on_task_id)} <small class="muted">(${esc(labels[blocker?.status] || "Task")})</small></span>
      ${isAdmin() ? `<button type="button" class="button button-outline" data-dependency-remove="${esc(d.id)}">Remove</button>` : ""}</div>`;
  }).join("");
  $("modal-fields").insertAdjacentHTML("beforeend", `
    <section class="comment-panel">
      <strong>Comments (${comments.length})</strong>
      ${commentRows || '<p class="muted">No comments yet.</p>'}
    </section>
    <section class="dependency-panel">
      <strong>Prerequisite tasks (${dependencies.length})</strong>
      <p class="muted">This task cannot move into In Progress, In Review, or Done until every prerequisite is DONE.</p>
      ${incompleteDependencies.length ? `<p class="error-text">Blocked by ${incompleteDependencies.length} incomplete prerequisite task(s). Complete them before advancing this task.</p>` : '<p class="success-text">All prerequisite tasks are complete.</p>'}
      ${dependencyNames || '<p class="muted">No prerequisites added.</p>'}
      ${isAdmin() && dependencyCandidates.length === 0 ? '<small class="muted">No other incomplete tasks are available in this workspace.</small>' : ""}
    </section>
    <p class="muted">Task version: ${task.version}. ${canManageDetails ? "Owners and admins can edit task details and manage its workflow." : canMoveAssignedTask ? "You can only comment or move your assigned task one step forward. Task details are read-only." : "Task details are read-only. You can still add a comment."}</p>
  `);
  document.querySelectorAll("[data-dependency-remove]").forEach(button => button.onclick = async () => {
    if (!confirm("Remove this prerequisite?")) return;
    try {
      await api(`/workspaces/${state.workspace.id}/tasks/${id}/dependencies/${button.dataset.dependencyRemove}`, { method: "DELETE" });
      toast("Dependency removed.");
      $("modal").close();
      await loadTasks();
      await showTask(id);
    } catch (error) { toast(error.message); }
  });
}

async function showMembers() {
  await loadWorkspaceMembers();
  const invitations = isAdmin()
    ? await api(`/workspaces/${state.workspace.id}/invitations`).catch(() => [])
    : [];
  const isOwner = state.role === "OWNER";
  $("toolbar").innerHTML = `
    <button class="button button-outline" id="members-back">← Workspace</button>
    <span class="muted">${state.members.length} workspace members</span>
    ${isAdmin() ? '<button class="button button-primary" id="invite-member">＋ Add registered member</button>' : ""}
    ${isOwner ? '<button class="button button-outline" id="transfer-owner">Transfer ownership</button>' : ""}`;
  $("members-back").onclick = renderWorkspace;
  if ($("invite-member")) $("invite-member").onclick = () => openModal("Add registered member", [
    { id: "email", label: "Registered account email", type: "email", required: true },
    { id: "role", label: "Workspace role", type: "select", value: "MEMBER",
      options: [{ value: "ADMIN", label: "Admin" }, { value: "MEMBER", label: "Member" }] },
  ], async data => {
    await api(`/workspaces/${state.workspace.id}/invitations`, json("POST", data));
    toast(`Invitation created for registered user ${data.email}.`);
    await showMembers();
  }, { saveLabel: "Invite member" });

  if ($("transfer-owner")) $("transfer-owner").onclick = () => {
    const candidates = state.members.filter(m => m.role !== "OWNER");
    if (!candidates.length) return toast("Add another registered member before transferring ownership.");
    openModal("Transfer workspace ownership", [
      { id: "new_owner_user_id", label: "New owner", type: "select", required: true,
        options: candidates.map(m => ({ value: m.user_id, label: `${m.name} (${m.email}) — ${m.role}` })) },
    ], async data => {
      await api(`/workspaces/${state.workspace.id}/transfer-ownership`, json("POST", data));
      toast("Workspace ownership transferred. Your role is now ADMIN.");
      await loadWorkspaceMembers();
      await showMembers();
    }, { saveLabel: "Transfer ownership" });
  };

  $("content").innerHTML = `
    <section class="panel">
      <h3>Workspace members</h3>
      ${state.members.map(m => {
        const isWorkspaceOwner = m.role === "OWNER";
        const canManageThisMember = isAdmin() && !isWorkspaceOwner && m.user_id !== state.user.id;
        return `<div class="member-row">
          <div><strong>${esc(m.name)}</strong><div class="muted">${esc(m.email)}</div></div>
          <div class="toolbar-actions">
            ${canManageThisMember ? `<select aria-label="Role for ${esc(m.name)}" data-role-select="${esc(m.user_id)}"><option value="MEMBER" ${m.role === "MEMBER" ? "selected" : ""}>Member</option><option value="ADMIN" ${m.role === "ADMIN" ? "selected" : ""}>Admin</option></select><button class="button button-outline" data-role-save="${esc(m.user_id)}">Update role</button><button class="button button-outline" data-member-remove="${esc(m.user_id)}">Remove</button>` : `<span class="pill">${m.role}</span>`}
            ${isOwner && !isWorkspaceOwner ? `<button class="button button-outline" data-transfer-to="${esc(m.user_id)}">Make owner</button>` : ""}
          </div>
        </div>`;
      }).join("")}
    </section>
    <section class="panel" style="margin-top:18px">
      <h3>Pending invitations</h3>
      ${invitations.filter(i => i.status === "PENDING").length
        ? invitations.filter(i => i.status === "PENDING").map(i => `<div class="member-row">
            <div><strong>${esc(i.email)}</strong><div class="muted">Expires ${new Date(i.expires_at).toLocaleDateString()}</div></div>
            <div class="toolbar-actions"><span class="pill">${i.role}</span><button class="button button-outline" data-invitation-revoke="${esc(i.id)}">Revoke</button></div>
          </div>`).join("")
        : '<p class="muted">No pending invitations.</p>'}
    </section>`;

  document.querySelectorAll("[data-role-save]").forEach(button => button.onclick = async () => {
    const userId = button.dataset.roleSave;
    const role = document.querySelector(`[data-role-select="${CSS.escape(userId)}"]`).value;
    try {
      await api(`/workspaces/${state.workspace.id}/members/${userId}`, json("PATCH", { role }));
      toast("Member role updated.");
      await showMembers();
    } catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-member-remove]").forEach(button => button.onclick = async () => {
    const userId = button.dataset.memberRemove;
    const member = state.members.find(m => m.user_id === userId);
    if (!confirm(`Remove ${member?.name || "this member"} from the workspace?`)) return;
    try {
      await api(`/workspaces/${state.workspace.id}/members/${userId}`, { method: "DELETE" });
      toast("Member removed from workspace.");
      await showMembers();
    } catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-transfer-to]").forEach(button => button.onclick = async () => {
    const member = state.members.find(m => m.user_id === button.dataset.transferTo);
    if (!confirm(`Transfer workspace ownership to ${member?.name}? Your role will become ADMIN.`)) return;
    try {
      await api(`/workspaces/${state.workspace.id}/transfer-ownership`, json("POST", { new_owner_user_id: button.dataset.transferTo }));
      toast("Workspace ownership transferred.");
      await loadWorkspaceMembers();
      await showMembers();
    } catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-invitation-revoke]").forEach(button => button.onclick = async () => {
    try {
      await api(`/workspaces/${state.workspace.id}/invitations/${button.dataset.invitationRevoke}`, { method: "DELETE" });
      toast("Invitation revoked.");
      await showMembers();
    } catch (error) { toast(error.message); }
  });
}

async function showInvitations() {
  setTitle("Invitations");
  $("add-project").classList.add("hidden");
  $("toolbar").innerHTML = `<span class="muted">Invitations sent to your account email</span>`;
  const invitations = await api("/workspaces/invitations/mine");
  $("content").innerHTML = invitations.length
    ? `<div class="panel">${invitations.map(i => {
        const workspace = state.workspaces.find(w => w.id === i.workspace_id);
        return `<div class="member-row">
          <div><strong>${esc(workspace?.name || "Workspace invitation")}</strong>
            <div class="muted">${esc(i.email)} · invited as ${i.role} · expires ${new Date(i.expires_at).toLocaleDateString()}</div>
          </div>
          <button class="button button-primary" data-accept="${esc(i.id)}" data-workspace="${esc(i.workspace_id)}">Accept invitation</button>
        </div>`;
      }).join("")}</div>`
    : `<div class="empty"><h3>No pending invitations</h3><p class="muted">New workspace invitations will appear here instantly.</p></div>`;
  document.querySelectorAll("[data-accept]").forEach(button => button.onclick = async () => {
    try {
      await api(`/workspaces/invitations/${button.dataset.accept}/accept`, json("POST", {}));
      toast("You joined the workspace.");
      await loadWorkspaces();
      const joinedWorkspace = state.workspaces.find(w => w.id === button.dataset.workspace);
      if (joinedWorkspace) await selectWorkspace(joinedWorkspace.id);
      await updateInvitationCount();
      await showInvitations();
    } catch (error) { toast(error.message); }
  });
}

async function showAuditLogs(projectId = null) {
  setTitle("Workspace activity");
  $("add-project").classList.add("hidden");
  const projectOptions = state.projects.map(p => `<option value="${esc(p.id)}" ${p.id === projectId ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  $("toolbar").innerHTML = `
    <button class="button button-outline" id="logs-back">← ${projectId ? "Project" : "Workspace"}</button>
    <label class="field">Filter by project
      <select id="audit-project"><option value="">All projects</option>${projectOptions}</select>
    </label>`;
  $("logs-back").onclick = projectId ? () => openProject(projectId) : renderWorkspace;
  $("audit-project").onchange = () => showAuditLogs($("audit-project").value || null);

  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}&limit=200` : "?limit=200";
  const logs = await api(`/workspaces/${state.workspace.id}/audit-logs${query}`);
  $("content").innerHTML = logs.length
    ? `<div class="panel">${logs.map(log => {
        const detail = log.after_data || log.before_data || {};
        const summary = Object.entries(detail).map(([k, v]) => `${k.replaceAll("_", " ")}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ");
        return `<article class="audit-row">
          <div class="audit-time">${new Date(log.created_at).toLocaleString()}</div>
          <div><div class="audit-action">${esc(log.action.replaceAll("_", " "))}</div>
            <div class="audit-detail">${esc(log.actor_name)} · ${esc(log.project_name || "Workspace-level activity")}${summary ? ` · ${esc(summary)}` : ""}</div>
          </div>
          <span class="pill">${esc(log.entity_type)}</span>
        </article>`;
      }).join("")}</div>`
    : `<div class="empty"><h3>No activity yet</h3><p class="muted">Workspace and project changes will appear here.</p></div>`;
}

async function showNotifications() {
  const items = await api("/notifications");
  $("add-project").classList.add("hidden");
  setTitle("Notifications");
  $("toolbar").innerHTML = `<span class="muted">Your activity inbox</span><button class="button button-outline" id="read-all">Mark all read</button>`;
  $("read-all").onclick = async () => {
    await api("/notifications/read-all", json("PATCH", {}));
    await showNotifications();
    await updateUnread();
  };
  $("content").innerHTML = items.length
    ? `<div class="panel">${items.map(n => `<div class="notification ${n.read_at ? "" : "unread"}">
        <div><p>${esc(n.message)}</p><small>${new Date(n.created_at).toLocaleString()}</small></div>
        ${n.read_at ? "" : `<button class="button button-outline" data-read="${esc(n.id)}">Mark read</button>`}
      </div>`).join("")}</div>`
    : `<div class="empty"><h3>You're all caught up</h3><p class="muted">New activity will appear here.</p></div>`;
  document.querySelectorAll("[data-read]").forEach(button => button.onclick = async () => {
    await api(`/notifications/${button.dataset.read}/read`, json("PATCH", {}));
    await showNotifications();
    await updateUnread();
  });
}

async function updateUnread() {
  try {
    const result = await api("/notifications/unread-count");
    $("unread-count").textContent = result.unread_count;
  } catch {}
}

async function updateInvitationCount() {
  try {
    const items = await api("/workspaces/invitations/mine");
    $("invitation-count").textContent = items.length;
  } catch {}
}

async function bootApp() {
  state.user = await api("/auth/me");
  $("user-label").textContent = state.user.name;
  await loadWorkspaces();
  if (state.workspaces.length) await selectWorkspace(state.workspaces[0].id);
  else await renderWorkspace();
  await updateUnread();
  await updateInvitationCount();
  if (Number($("invitation-count").textContent) > 0) {
    toast(`You have ${$("invitation-count").textContent} pending workspace invitation(s).`);
  }
  await connectEventStream();
}

$("new-workspace").onclick = createWorkspace;
$("add-project").onclick = createProject;
$("notifications-button").onclick = showNotifications;
$("invitations-button").onclick = showInvitations;
$("logout-button").onclick = async () => {
  try { await api("/auth/logout", json("POST", {})); } catch {}
  if (state.eventSource) state.eventSource.close();
  clearAccessToken();
  location.assign("/");
};

(async () => {
  if (!(await refresh())) {
    location.replace("/login");
    return;
  }
  try {
    await bootApp();
  } catch (error) {
    toast(error.message || "Unable to load your workspace.");
    setTimeout(() => location.replace("/login"), 1200);
  }
})();
