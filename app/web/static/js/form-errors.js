/**
 * Focus la primul camp invalid + evidentiere inline (issue #48). Functioneaza
 * generic pentru orice bloc `[data-error-summary]` (vezi
 * partials/_form_errors.html) -- fiecare `<li data-error-field="...">` e
 * potrivit dupa `name` cu inputul corespunzator din pagina.
 */
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-error-summary]").forEach(function (summaryEl) {
    var firstInvalid = null;
    summaryEl.querySelectorAll("[data-error-field]").forEach(function (li) {
      var fieldName = li.getAttribute("data-error-field");
      if (!fieldName) return;
      var input = document.querySelector('[name="' + fieldName.replace(/"/g, '\\"') + '"]');
      if (!input) return;
      input.setAttribute("aria-invalid", "true");
      input.classList.add("input-invalid");
      if (!firstInvalid) firstInvalid = input;
    });
    if (firstInvalid) {
      firstInvalid.focus();
    } else {
      summaryEl.focus();
    }
  });
});
