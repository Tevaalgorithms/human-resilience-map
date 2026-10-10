// Clientside helpers (no network): theme handling.
window.dash_clientside = Object.assign({}, window.dash_clientside, {
  ui: {
    // Runs on load (stored theme or OS preference) and on every toggle click.
    theme: function (nClicks, stored) {
      var ctx = window.dash_clientside.callback_context || {};
      var current = stored || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      if (ctx.triggered_id === "theme-toggle") current = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", current);
      return current;
    },
  },
});
