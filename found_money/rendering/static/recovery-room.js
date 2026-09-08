document.documentElement.dataset.enhanced = "true";
const roomLinks = [...document.querySelectorAll('nav a[href^="#"]')];
function markRoom(id) {
  document.documentElement.dataset.activeRoom = id;
  for (const link of roomLinks) {
    if (link.hash === `#${id}`) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}
for (const link of roomLinks) link.addEventListener("click", () => markRoom(link.hash.slice(1)));
markRoom(location.hash.slice(1) || "room-find");
if ("IntersectionObserver" in window) {
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) markRoom(entry.target.id);
  }, { rootMargin: "-10% 0px -65% 0px" });
  document.querySelectorAll("[data-room]").forEach(room => observer.observe(room));
}
for (const button of document.querySelectorAll("button.copy-email")) {
  button.disabled = false;
  const feedback = document.createElement("span");
  feedback.className = "copy-feedback";
  feedback.setAttribute("role", "status");
  button.after(feedback);
  button.addEventListener("click", async () => {
    const item = button.closest(".play-email");
    if (!item) return;
    const payload = ["subject", "body", "cta"]
      .map(field => (item.querySelector(`.play-email-${field}`)?.textContent || "").trim())
      .filter(Boolean).join("\n\n");
    button.disabled = true;
    feedback.textContent = "";
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(payload);
      button.textContent = "Copied";
      button.classList.add("copied");
      feedback.textContent = "Email copied to clipboard.";
    } catch {
      button.textContent = "Copy email";
      button.classList.remove("copied");
      feedback.textContent = "Copy unavailable. Select the email text and copy it manually.";
    } finally {
      button.disabled = false;
    }
  });
}
const angleLinks = [...document.querySelectorAll(".play-angles a[href^='#']")];
function selectPlay(id) {
  if (!document.getElementById(id)?.matches(".play-campaign")) return;
  for (const link of angleLinks) {
    if (link.hash === `#${id}`) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  }
  for (const play of document.querySelectorAll(".play-campaign")) {
    play.classList.toggle("play-selected", play.id === id);
  }
  document.documentElement.dataset.playChooser = "true";
}
if (angleLinks.length) {
  selectPlay(angleLinks[0].hash.slice(1));
  for (const link of angleLinks) link.addEventListener("click", () => selectPlay(link.hash.slice(1)));
  const selectHashPlay = () => {
    const target = document.getElementById(location.hash.slice(1));
    const play = target?.closest(".play-campaign");
    if (play) { selectPlay(play.id); target.scrollIntoView(); }
  };
  window.addEventListener("hashchange", selectHashPlay);
  selectHashPlay();
}
