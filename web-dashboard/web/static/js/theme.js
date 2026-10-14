/* Dark mode.
 *
 * Loaded as a classic script in <head> so the stored preference is applied
 * before first paint; a dark-mode user must never see a light flash.
 *
 * The choice is per browser, so localStorage is the right home for it: it is a
 * viewing preference, not application state, and losing it costs nothing.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "freshguard-theme";

  function stored() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      // Private browsing or blocked storage. Fall back to the system setting.
      return null;
    }
  }

  function prefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  /* An explicit ?theme=light or ?theme=dark in the address wins and is then
   * remembered, so "open this in dark mode" means what it says. It is also
   * the only way to photograph a chosen theme: headless Chrome reports its
   * own colour-scheme preference and ignores every flag offered to change it. */
  function requested() {
    try {
      var asked = new URLSearchParams(window.location.search).get("theme");
      return asked === "light" || asked === "dark" ? asked : null;
    } catch (error) {
      return null;
    }
  }

  function remember(theme) {
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch (error) {
      // Preference simply will not persist; the page still works.
    }
  }

  function apply(theme) {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }

  function current() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  var asked = requested();
  if (asked) {
    remember(asked);
  }
  apply(asked || stored() || (prefersDark() ? "dark" : "light"));

  // Exposed so app.js can wire the toggle once the DOM exists.
  window.freshguardTheme = {
    current: current,
    toggle: function () {
      var next = current() === "dark" ? "light" : "dark";
      apply(next);
      remember(next);
      return next;
    },
  };
})();
