window.addEventListener("load", () => {
  const inputs = document.querySelectorAll("input,select,textarea");
  if (inputs.length > 0) {
    for (let i = 0, t = inputs.length; i < t; i++) {
      if (inputs[i].parentNode.className.indexOf("submit-row") === -1) {
        inputs[i].addEventListener("change", () => {
          window.onbeforeunload = () => "Your changes have not been saved.";
        });
      } else {
        inputs[i].addEventListener("click", () => {
          window.onbeforeunload = null;
        });
      }
    }
  }
});
