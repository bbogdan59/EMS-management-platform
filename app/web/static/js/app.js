function emsToggleTheme() {
  const root = document.documentElement;
  const isDark = root.classList.toggle("dark");
  try {
    localStorage.setItem("ems-theme", isDark ? "dark" : "light");
  } catch (e) {
    /* localStorage indisponibil (mod privat) -- tema ramane doar pentru sesiunea curenta */
  }
}

/**
 * Conexiune SSE cu reconectare automata si backoff exponential plafonat.
 * Foloseste EventSource nativ; la eroare/inchidere reincearca la 1s, 2s, 4s...
 * pana la maxDelayMs, apoi ramane pe acel interval.
 */
function emsConnectSSE(url, handlers) {
  let delay = 1000;
  const maxDelay = 30000;
  let source = null;
  let closedByUser = false;

  function connect() {
    source = new EventSource(url);
    source.onopen = () => {
      delay = 1000;
      if (handlers.onopen) handlers.onopen();
    };
    for (const [eventName, fn] of Object.entries(handlers.events || {})) {
      source.addEventListener(eventName, (ev) => fn(ev));
    }
    source.onerror = () => {
      if (handlers.onerror) handlers.onerror();
      source.close();
      if (!closedByUser) {
        setTimeout(connect, delay);
        delay = Math.min(delay * 2, maxDelay);
      }
    };
  }

  connect();

  return {
    close() {
      closedByUser = true;
      if (source) source.close();
    },
  };
}
