/* Receipt check. Runs entirely in the browser: the receipt code is hashed here
 * (SHA-256 of "rcpt/v1" + election id + code, as in ballot.js) and compared with
 * the published list. The code is never sent to the server. */
(function () {
  "use strict";
  var form = document.getElementById("receipt-check");
  if (!form || !window.crypto || !window.crypto.subtle) { return; }
  form.hidden = false;
  var election = form.getAttribute("data-election");
  var input = document.getElementById("receipt-code-input");
  var result = document.getElementById("receipt-result");

  function normalise(code) { return code.replace(/[\s-]/g, "").toUpperCase(); }
  function hex(buffer) {
    return Array.prototype.map.call(new Uint8Array(buffer), function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }
  var listed = {};
  document.querySelectorAll("#commitments code").forEach(function (el) {
    listed[el.textContent.trim()] = el;
  });
  try {
    var remembered = window.localStorage.getItem("receipt:" + election);
    if (remembered && window.localStorage.getItem("receipt-ok:" + election)) {
      input.value = remembered.replace(/(.{4})(?=.)/g, "$1-");
    }
  } catch (e) { /* storage unavailable */ }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var code = normalise(input.value);
    if (code.length !== 16) { result.textContent = "A receipt code has 16 characters."; return; }
    var data = new TextEncoder().encode("rcpt/v1" + election + code);
    window.crypto.subtle.digest("SHA-256", data).then(function (digest) {
      var match = listed[hex(digest)];
      document.querySelectorAll("#commitments li.match").forEach(function (li) {
        li.classList.remove("match");
      });
      if (match) {
        match.parentNode.classList.add("match");
        result.textContent = "Found: your ballot is in the ballot box.";
      } else {
        result.textContent = "Not found. Check the code; if it is correct, contact the " +
          "Returning Officer.";
      }
    });
  });
}());
