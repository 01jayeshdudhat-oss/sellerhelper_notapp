(function () {
    "use strict";

    var choiceKey = "sellerhelper-pwa-choice";
    var prompt = document.getElementById("pwa-install-prompt");
    var installButton = document.getElementById("pwa-install-action");
    var iosInstalledButton = document.getElementById("pwa-ios-installed");
    var iosSteps = document.getElementById("pwa-ios-steps");
    var deferredInstallPrompt = null;

    if (!prompt) {
        return;
    }

    function getChoice() {
        try {
            return window.localStorage.getItem(choiceKey);
        } catch (error) {
            return null;
        }
    }

    function saveChoice(choice) {
        try {
            window.localStorage.setItem(choiceKey, choice);
        } catch (error) {
            // Keep the prompt usable when browser storage is unavailable.
        }
        prompt.hidden = true;
    }

    function isStandalone() {
        return window.matchMedia("(display-mode: standalone)").matches ||
            window.navigator.standalone === true;
    }

    function showNativePrompt() {
        prompt.hidden = false;
        installButton.hidden = false;
        iosInstalledButton.hidden = true;
        iosSteps.hidden = true;
    }

    function showIosInstructions() {
        prompt.hidden = false;
        installButton.hidden = true;
        iosInstalledButton.hidden = false;
        iosSteps.hidden = false;
    }

    function isIosDevice() {
        var platform = window.navigator.platform || "";
        var userAgent = window.navigator.userAgent || "";
        return /iPad|iPhone|iPod/.test(platform + userAgent) ||
            (platform === "MacIntel" && window.navigator.maxTouchPoints > 1);
    }

    if ("serviceWorker" in window.navigator && window.isSecureContext) {
        window.navigator.serviceWorker.register("/service-worker.js", { scope: "/" })
            .catch(function () {
                // Installation remains available even if service-worker setup fails.
            });
    }

    if (getChoice() || isStandalone()) {
        return;
    }

    if (isIosDevice()) {
        showIosInstructions();
    }

    window.addEventListener("beforeinstallprompt", function (event) {
        if (getChoice() || isStandalone()) {
            return;
        }
        event.preventDefault();
        deferredInstallPrompt = event;
        showNativePrompt();
    });

    window.addEventListener("appinstalled", function () {
        saveChoice("installed");
    });

    prompt.querySelectorAll("[data-pwa-dismiss]").forEach(function (button) {
        button.addEventListener("click", function () {
            saveChoice("dismissed");
        });
    });

    installButton.addEventListener("click", function () {
        if (!deferredInstallPrompt) {
            return;
        }

        deferredInstallPrompt.prompt();
        deferredInstallPrompt.userChoice.then(function (result) {
            deferredInstallPrompt = null;
            saveChoice(result && result.outcome === "accepted" ? "installed" : "dismissed");
        }).catch(function () {
            deferredInstallPrompt = null;
            saveChoice("dismissed");
        });
    });

    iosInstalledButton.addEventListener("click", function () {
        saveChoice("installed");
    });
})();
