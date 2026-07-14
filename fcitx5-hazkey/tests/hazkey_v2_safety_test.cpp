#include <cstdlib>
#include <iostream>
#include <string>

#include "hazkey_effect_ledger.h"
#include "hazkey_recovery_journal.h"

namespace {
[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void expect(bool value, const std::string& message) {
    if (!value) fail(message);
}

void effectLedgerDeduplicatesEffects() {
    HazkeyEffectLedger ledger(2);
    expect(ledger.claim(10), "first effect must be claimable");
    expect(!ledger.claim(10), "duplicate effect must be ignored");
    expect(ledger.claim(11), "second effect must be claimable");
    expect(ledger.claim(12), "newer effects must remain claimable");
    expect(!ledger.claim(11),
           "the bounded ledger must never forget an already applied effect");
    expect(!ledger.claim(0), "effect ID zero is invalid");
}

void journalDeduplicatesAndAcknowledgesRequests() {
    HazkeyRecoveryJournal journal(2);
    expect(journal.record({"request-a", "payload-a", 1, "session-a", false}),
           "first request must fit");
    expect(!journal.record(
               {"request-a", "payload-a-retry", 1, "session-a", false}),
           "the same request ID must reject a different wire envelope");
    expect(journal.record(
               {"request-a", "payload-a", 1, "session-a", true}),
           "an exact idempotency binding is already durable");
    expect(journal.record({"request-b", "payload-b", 2, "session-a", false}),
           "second request must fit");
    expect(journal.pending().size() == 2, "duplicate request must not be journaled twice");
    expect(!journal.record({"request-c", "payload-c", 3, "session-a", false}),
           "a full journal must reject rather than evict its head");
    expect(journal.pending().front().requestID == "request-a",
           "capacity pressure must preserve the oldest semantic action");
    expect(journal.markSent("request-a"), "recorded entry can be marked sent");
    expect(!journal.rebaseUnsent("request-a", "changed", 9, "session-b"),
           "a sent idempotency binding must be immutable");
    expect(journal.replace(
               "request-a",
               {"request-a-fresh", "payload-a", 4, "session-a", false}),
           "a stale binding must be replaceable in place with a fresh ID");
    expect(journal.pending().front().requestID == "request-a-fresh",
           "replacement must preserve semantic order");
    journal.acknowledge("request-a-fresh");
    expect(journal.pending().size() == 1, "ack must remove one request");
    journal.confirmSnapshot("snapshot");
    expect(journal.lastSnapshot() == "snapshot", "last snapshot must be retained");
    journal.clear();
    expect(journal.pending().empty(), "clear must discard pending requests");
    expect(journal.lastSnapshot().empty(), "clear must discard the snapshot");
}
}  // namespace

int main() {
    effectLedgerDeduplicatesEffects();
    journalDeduplicatesAndAcknowledgesRequests();
    return 0;
}
