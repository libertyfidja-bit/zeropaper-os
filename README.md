# 🌍 ZeroPaper Health & Building OS (`zeropaper-os`)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Flask 3.0](https://img.shields.io/badge/flask-3.0-green.svg)](https://flask.palletsprojects.com/)
[![Docker](https://img.shields.io/badge/docker-on--premise-blueviolet.svg)](https://www.docker.com/)
[![Pytest](https://img.shields.io/badge/pytest-passing-success.svg)](https://pytest.org/)
[![License: Proprietary](https://img.shields.io/badge/license-commercial-red.svg)](#license)

**ZeroPaper OS** is an enterprise-grade, self-hosted check-in, queue triage, cashier billing, and clinical record operating system designed for hospitals, health centers, and high-traffic public/private buildings. It completely eliminates paper logbooks, clipboards, and printed prescriptions while enforcing strict **local data residency** (Bring Your Own Cloud / On-Premise Server) and automated disaster recovery.

---

## 🏛️ System Architecture & Data Residency

Unlike traditional cloud SaaS that stores sensitive patient records on third-party servers, ZeroPaper OS runs entirely on the hospital's **own local server hardware or private data center**. 

```text
  [ NFC Card / Wristband ] ──┐
  [ Self-Service Kiosk ]   ──┼──> [ Hospital On-Premise Server ] ──> [ Local SQLite / PostgreSQL DB ]
  [ USSD / SMS *250# ]     ──┤         (Docker Container)         ──> [ Encrypted Daily .db.gz Backups ]
  [ National ID OCR Scan ] ──┘
