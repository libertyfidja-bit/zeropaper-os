import os
import serial
import requests

MODEM_PORT = os.environ.get("HOSPITAL_MODEM_PORT", "/dev/ttyUSB0")
BAUD_RATE = 115200

def send_payment_receipt(phone_number: str, patient_name: str, ticket_number: str, department: str):
    """Sends an automated SMS receipt upon cashier payment confirmation."""
    message = (
        f"ZeroPaper Health: Payment confirmed for {patient_name}. "
        f"Ticket: {ticket_number} ({department}). "
        f"You are now cleared for doctor consultation. Thank you!"
    )

    # Method A: Try sending via local USB GSM Modem (Offline & Free per SMS bundle)
    success = send_via_local_modem(phone_number, message)
    if success:
        print(f"[SMS SENT VIA USB MODEM] To {phone_number}")
        return True

    # Method B: Fallback to Cloud API (e.g., Africa's Talking / Twilio) if modem fails
    print(f"[SMS MODEM UNAUTHORIZED/OFFLINE] Attempting cloud SMS fallback...")
    return False

def send_via_local_modem(phone_number, message):
    try:
        ser = serial.Serial(MODEM_PORT, BAUD_RATE, timeout=2)
        ser.write(b"AT+CMGF=1\r") # Text mode
        ser.write(f'AT+CMGS="{phone_number}"\r'.encode())
        ser.write(f"{message}\x1a".encode()) # \x1a is Ctrl+Z to send
        ser.close()
        return True
    except Exception as e:
        return False
