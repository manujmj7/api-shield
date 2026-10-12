# api-shield — Flask API Rate Limiting System

API Shield is a lightweight, high-performance API protection middleware built with Python, Flask, and Redis. It defends backend endpoints against abusive traffic, denial-of-service attempts, and brute-force spikes by enforcing granular client-level rate limits.

---

## Features

- **Sliding Window & Fixed Window Rate Limiting:** Enforces rate thresholds dynamically with atomic Redis operations.
- **Client Identification:** Tracks requests per client IP address and API key tokens.
- **RFC-Compliant Headers:** Returns standard HTTP headers (`Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`).
- **HTTP 429 Status Handling:** Drops excessive requests gracefully before hitting downstream backend services.
- **Automated Test Suite:** Built-in integration tests using `pytest` and `fakeredis`.

---

## Tech Stack

- **Backend Framework:** Python 3, Flask
- **In-Memory Store:** Redis / `fakeredis`
- **Testing:** `pytest`, `coverage`

---

## Getting Started

### Prerequisites

- Python 3.10+
- Running Redis instance (or local Redis container)

### Installation

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/manujmj7/api-shield.git](https://github.com/manujmj7/api-shield.git)
   cd api-shield
