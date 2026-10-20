// (function(){
//   function loadScript(path){
//     var s=document.createElement('script');
//     s.src = (document.body.dataset.staticUrl || '/static/') + path;
//     s.defer = true;
//     document.body.appendChild(s);
//   }

//   function pick(){
//     var w = window.innerWidth || document.documentElement.clientWidth;
//     if(w>=992){ loadScript('seller/js/desktop.js'); }
//     else { loadScript('seller/js/mobile.js'); }
//   }

//   // initial load
//   document.addEventListener('DOMContentLoaded', function(){
//     pick();
//     var body = document.body;
//     var navToggle = document.getElementById('navModeToggle');
//     var sidebarToggle = document.getElementById('sidebarModeToggle');

//     function toggleCompact(){
//       body.classList.toggle('sidebar-compact');
//     }

    

//     var sidebarNavToggle = document.getElementById('sidebarNavToggle');
//     if(sidebarNavToggle){
//       sidebarNavToggle.addEventListener('click', function(){
//         document.body.classList.toggle('nav-top');
//         navToggle.textContent = document.body.classList.contains('nav-top') ? 'Use sidebar nav' : 'Use top nav';
//         sidebarNavToggle.textContent = document.body.classList.contains('nav-top') ? 'Use sidebar nav' : 'Use top nav';
//       });
//     }
//     if(navToggle){
//       navToggle.addEventListener('click', function(){
//         document.body.classList.toggle('nav-top');
//         navToggle.textContent = document.body.classList.contains('nav-top') ? 'Use sidebar nav' : 'Use top nav';
//         var sidebarNavToggle = document.getElementById('sidebarNavToggle');
//         if(sidebarNavToggle){
//           sidebarNavToggle.textContent = document.body.classList.contains('nav-top') ? 'Use sidebar nav' : 'Use top nav';
//         }
//       });
//     }

//     if(sidebarToggle){
//       sidebarToggle.addEventListener('click', function(){
//         toggleCompact();
//       });
//     }

//     var dashboardToggle = document.getElementById('dashboardToggle');
//     var dashboardSubnav = document.getElementById('dashboardSubnav');
//     if(dashboardToggle && dashboardSubnav){
//       dashboardToggle.addEventListener('click', function(){
//         var open = dashboardSubnav.classList.toggle('open');
//         dashboardToggle.classList.toggle('active', open);
//       });
//     }
//   });

//   // optional: reload on resize after debounce
//   var resizeTimer;
//   window.addEventListener('resize', function(){
//     clearTimeout(resizeTimer);
//     resizeTimer = setTimeout(function(){ pick(); }, 250);
//   });
// })();

(function () {

    /* =========================================================
       LOAD DESKTOP / MOBILE JS
    ========================================================= */

    function loadScript(path) {

        var s = document.createElement("script");

        s.src =
            (document.body.dataset.staticUrl || "/static/")
            + path;

        s.defer = true;

        document.body.appendChild(s);
    }


    function pick() {

        var w =
            window.innerWidth ||
            document.documentElement.clientWidth;

        if (w >= 992) {

            loadScript("seller/js/desktop.js");

        } else {

            loadScript("seller/js/mobile.js");

        }
    }


    /* =========================================================
       NAVIGATION
       
       DEFAULT:
       TOP NAVIGATION

       Saved:
       "top"
       "sidebar"
    ========================================================= */

    function initNavigation() {

        var body =
            document.body;

        var sidebar =
            document.getElementById("sidebarCol");

        var mainContent =
            document.getElementById("mainContent");

        var navToggle =
            document.getElementById("navModeToggle");

        var sidebarNavToggle =
            document.getElementById("sidebarNavToggle");

        var sidebarToggle =
            document.getElementById("sidebarModeToggle");


        /* -----------------------------------------
           Update button text
        ----------------------------------------- */

        function updateButtonText(mode) {

            if (navToggle) {

                if (mode === "sidebar") {

                    navToggle.textContent =
                        "Use top nav";

                } else {

                    navToggle.textContent =
                        "Use sidebar nav";
                }
            }


            if (sidebarNavToggle) {

                if (mode === "sidebar") {

                    sidebarNavToggle.textContent =
                        "Use top nav";

                } else {

                    sidebarNavToggle.textContent =
                        "Use sidebar nav";
                }
            }
        }


        /* -----------------------------------------
           TOP NAVIGATION
        ----------------------------------------- */

        function showTopNav() {

            body.classList.add("nav-top");

            body.classList.remove("nav-sidebar");


            if (sidebar) {

                sidebar.style.display = "none";
            }


            if (mainContent) {

                mainContent.classList.remove(
                    "col-lg-9"
                );

                mainContent.classList.add(
                    "col-12"
                );
            }


            localStorage.setItem(
                "navMode",
                "top"
            );


            updateButtonText("top");
        }


        /* -----------------------------------------
           SIDEBAR NAVIGATION
        ----------------------------------------- */

        function showSidebar() {

            body.classList.remove("nav-top");

            body.classList.add("nav-sidebar");


            if (sidebar) {

                sidebar.style.display = "block";
            }


            if (mainContent) {

                mainContent.classList.remove(
                    "col-12"
                );

                mainContent.classList.add(
                    "col-lg-9"
                );
            }


            localStorage.setItem(
                "navMode",
                "sidebar"
            );


            updateButtonText("sidebar");
        }


        /* -----------------------------------------
           TOP NAV BUTTON
        ----------------------------------------- */

        if (navToggle) {

            navToggle.addEventListener(
                "click",
                function () {

                    var currentMode =
                        localStorage.getItem(
                            "navMode"
                        );


                    if (currentMode === "sidebar") {

                        showTopNav();

                    } else {

                        showSidebar();

                    }

                }
            );
        }


        /* -----------------------------------------
           SIDEBAR TOP NAV BUTTON
        ----------------------------------------- */

        if (sidebarNavToggle) {

            sidebarNavToggle.addEventListener(
                "click",
                function () {

                    showTopNav();

                }
            );
        }


        /* -----------------------------------------
           SIDEBAR COMPACT BUTTON
        ----------------------------------------- */

        if (sidebarToggle) {

            sidebarToggle.addEventListener(
                "click",
                function () {

                    body.classList.toggle(
                        "sidebar-compact"
                    );

                }
            );
        }


        /* -----------------------------------------
           RESTORE SAVED NAVIGATION
           
           IMPORTANT:
           DEFAULT = TOP
        ----------------------------------------- */

        var savedMode =
            localStorage.getItem(
                "navMode"
            );


        if (savedMode === "sidebar") {

            showSidebar();

        } else {

            showTopNav();

        }
    }


    /* =========================================================
       DASHBOARD SUB NAVIGATION
    ========================================================= */

    function initDashboardNavigation() {

        var dashboardToggle =
            document.getElementById(
                "dashboardToggle"
            );

        var dashboardSubnav =
            document.getElementById(
                "dashboardSubnav"
            );


        if (
            !dashboardToggle ||
            !dashboardSubnav
        ) {

            return;
        }


        dashboardToggle.addEventListener(
            "click",
            function () {

                var open =
                    dashboardSubnav.classList.toggle(
                        "open"
                    );


                dashboardToggle.classList.toggle(
                    "active",
                    open
                );

            }
        );
    }


    /* =========================================================
       INITIAL LOAD
    ========================================================= */

    document.addEventListener(
        "DOMContentLoaded",
        function () {

            pick();

            initNavigation();

            initDashboardNavigation();

        }
    );


    /* =========================================================
       RESIZE
    ========================================================= */

    var resizeTimer;


    window.addEventListener(
        "resize",
        function () {

            clearTimeout(
                resizeTimer
            );


            resizeTimer = setTimeout(
                function () {

                    pick();

                },
                250
            );

        }
    );

})();

