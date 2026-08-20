// The pencil beside a run's name swaps the heading for the input that edits it.
// Delegated from the document, like row-link.js: the run page draws this header on
// load and the poller redraws parts of it afterwards.
document.addEventListener("click", function (event) {
  var pencil = event.target.closest(".run-name-edit");
  if (!pencil) return;
  var title = pencil.closest(".run-title");
  var form = title.querySelector(".run-name-form");
  // The heading goes while the form is up, so the name is not on screen twice.
  title.classList.add("editing");
  form.hidden = false;
  form.querySelector("input").focus();
});
