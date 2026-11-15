import { request, setAccessToken } from "./api.js";

const $ = (id) => document.getElementById(id);
let mode = location.pathname === "/register" ? "register" : "login";

function renderMode() {
  const registering = mode === "register";
  $("auth-title").textContent = registering ? "Create your account" : "Welcome back";
  $("auth-subtitle").textContent = registering
    ? "Set up your account to join or create a workspace."
    : "Sign in to continue to your workspace.";
  $("name-field").classList.toggle("hidden", !registering);
  $("auth-name").required = registering;
  $("auth-password").autocomplete = registering ? "new-password" : "current-password";
  $("auth-submit").textContent = registering ? "Create account" : "Sign in";
  $("switch-copy").textContent = registering ? "Already have an account?" : "New to TaskFlow?";
  $("toggle-auth").textContent = registering ? "Sign in" : "Create an account";
  $("toggle-auth").href = registering ? "/login" : "/register";
  history.replaceState({}, "", registering ? "/register" : "/login");
}

$("toggle-auth").addEventListener("click", (event) => {
  event.preventDefault();
  mode = mode === "login" ? "register" : "login";
  $("auth-error").textContent = "";
  renderMode();
});

$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("auth-error").textContent = "";
  $("auth-submit").disabled = true;
  try {
    const email = $("auth-email").value.trim().toLowerCase();
    const password = $("auth-password").value;
    if (mode === "register") {
      await request("/auth/register", {
        method: "POST",
        body: JSON.stringify({
          name: $("auth-name").value.trim(),
          email,
          password,
        }),
      });
    }
    const result = await request("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setAccessToken(result.access_token);
    location.assign("/app");
  } catch (error) {
    $("auth-error").textContent = error.message || "Unable to authenticate.";
  } finally {
    $("auth-submit").disabled = false;
  }
});

renderMode();
