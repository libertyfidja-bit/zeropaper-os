/**
 * ZeroPaper OS — Real Hardware USB NFC & Optical ID Barcode Auto-Listener
 * Supports:
 *  1. USB HID Keyboard-Wedge Readers (NFC 13.56MHz UIDs & 1D/2D ID Barcodes)
 *  2. WebSerial API for direct COM/RS232 Biometric & MRZ OCR Scanners
 */

(function () {
  let scanBuffer = "";
  let lastKeyTime = Date.now();
  const MAX_INTERKEY_DELAY_MS = 45; // Hardware scanners type < 45ms per key; humans type > 100ms

  /**
   * Classifies scanned payload and fires instant check-in to local Flask server
   */
  async function processHardwareScan(rawCode) {
    const code = rawCode.trim();
    if (code.length < 4) return;

    // Pattern Detection:
    // - 8 to 14 hex characters or colon-separated hex = NFC Smart Card / Wristband UID
    // - 16 digits or MRZ string = National ID / Insurance OCR Scan
    const isNfcHex = /^[0-9A-Fa-f:]{8,20}$/.test(code) && !/^\d{16}$/.test(code);
    const checkinMethod = isNfcHex ? "NFC_CARD" : "ID_OCR";

    // Read currently selected default department from the active terminal screen
    const deptSelect = document.getElementById(isNfcHex ? "nfcDept" : "ocrDept");
    const department = deptSelect ? deptSelect.value : "General Consultation";

    const payload = {
      method: checkinMethod,
      identifier: code,
      full_name: isNfcHex
        ? `NFC Cardholder (${code.slice(-4).toUpperCase()})`
        : `Verified ID Holder (${code.slice(-4)})`,
      department: department,
      priority: "Standard"
    };

    try {
      const response = await fetch("/api/v1/checkin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const result = await response.json();

      showHardwareToast(
        `⚡ ${checkinMethod} DETECTED: Checked in as Ticket ${result.ticket_number} (${result.department})`
      );

      if (typeof refreshDashboard === "function") {
        refreshDashboard();
      }
    } catch (err) {
      console.error("Hardware Check-In Error:", err);
    }
  }

  /**
   * 1. Global High-Speed USB HID Keyboard-Wedge Listener
   */
  window.addEventListener("keydown", (e) => {
    // Ignore if staff is manually typing inside an input box slowly
    const now = Date.now();
    const timeDiff = now - lastKeyTime;

    if (timeDiff > MAX_INTERKEY_DELAY_MS) {
      scanBuffer = ""; // Reset buffer if keystrokes are slow (human typing)
    }
    lastKeyTime = now;

    if (e.key === "Enter") {
      if (scanBuffer.length >= 6) {
        e.preventDefault();
        const capturedCode = scanBuffer;
        scanBuffer = "";
        processHardwareScan(capturedCode);
      }
      return;
    }

    if (e.key.length === 1) {
      scanBuffer += e.key;
    }
  });

  /**
   * 2. WebSerial API Listener for USB COM-Port Optical / Biometric Scanners
   */
  window.connectSerialScanner = async function () {
    if (!("serial" in navigator)) {
      alert("WebSerial requires Chrome or Edge on the Kiosk terminal.");
      return;
    }
    try {
      const port = await navigator.serial.requestPort();
      await port.open({ baudRate: 9600 });
      showHardwareToast("🔌 USB Serial Scanner Connected & Listening...");

      const textDecoder = new TextDecoderStream();
      port.readable.pipeTo(textDecoder.writable);
      const reader = textDecoder.readable.getReader();

      let serialBuffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) {
          serialBuffer += value;
          if (serialBuffer.includes("\n") || serialBuffer.includes("\r")) {
            const cleanScan = serialBuffer.trim();
            serialBuffer = "";
            processHardwareScan(cleanScan);
          }
        }
      }
    } catch (err) {
      console.error("Serial Scanner Connection Failed:", err);
    }
  };

  /**
   * Visual On-Screen Kiosk Feedback Toast when a card/ID is tapped
   */
  function showHardwareToast(message) {
    let toast = document.getElementById("hwScanToast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "hwScanToast";
      toast.style.cssText =
        "position:fixed;bottom:24px;right:24px;background:#10b981;color:#052e16;" +
        "padding:14px 20px;border-radius:8px;font-weight:700;font-size:14px;" +
        "box-shadow:0 10px 25px rgba(0,0,0,0.4);z-index:9999;transition:opacity 0.3s;";
      document.body.appendChild(toast);
    }
    toast.innerText = message;
    toast.style.opacity = "1";
    setTimeout(() => {
      toast.style.opacity = "0";
    }, 3500);
  }
})();
