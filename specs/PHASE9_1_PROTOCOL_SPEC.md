# Phase 9.1 Spec: The Nervous System (Protocol & Auth)

## 1. Transport Layer
All Hive communication occurs over **BOLT 8** (Encrypted Lightning Connection).
*   **Mechanism:** `sendcustommsg` RPC.
*   **Message ID Range:** `32769` - `33000` (Odd numbers to allow ignoring by non-Hive peers).

## 2. Authentication: PKI & Manifests
To prevent shared-secret fragility, The Hive uses **Signed Manifests**.

### 2.1 The Invitation (Ticket)
An Admin Node generates a signed blob.
*   **Command:** `revenue-hive-invite --valid-hours=24 --req-splice`
*   **Payload:** `[Admin_Pubkey + Requirements_Bitmask + Expiration_Timestamp + Admin_Signature]`

### 2.2 The Handshake Flow
When Candidate (A) connects to Member (B):

1.  **A -> B (`HIVE_HELLO`):** Sends the **Ticket**.
2.  **B -> A (`HIVE_CHALLENGE`):** Sends a random 32-byte `Nonce`.
3.  **A -> B (`HIVE_ATTEST`):** Sends a **Signed Manifest**:
    ```json
    {
      "pubkey": "Node_A_Key",
      "version": "cl-revenue-ops v1.4.2",
      "features": ["splice", "dual-fund"],
      "nonce_reply": "signed_nonce"
    }
    ```
4.  **B (Verification):**
    *   Checks Ticket validity (Admin Sig + Expiry).
    *   Checks Manifest Signature (Identity Proof).
    *   **Active Probe:** B attempts a harmless technical negotiation (e.g., `splice_init`) to verify A actually supports the claimed features.
5.  **B -> A (`HIVE_WELCOME`):** Session established.

## 3. Message Types
| ID | Name | Payload |
| :--- | :--- | :--- |
| 32769 | `HIVE_HELLO` | Ticket |
| 32771 | `HIVE_CHALLENGE` | Nonce |
| 32773 | `HIVE_ATTEST` | Manifest + Sig |
| 32775 | `HIVE_WELCOME` | HiveID |
| 32777 | `HIVE_GOSSIP` | State Update (See 9.2) |
| 32779 | `HIVE_INTENT` | Lock Request (See 9.2) |
