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
    journal.record({"request-a", "payload-a", 1});
    journal.record({"request-a", "payload-a-retry", 1});
    journal.record({"request-b", "payload-b", 2});
    expect(journal.pending().size() == 2, "duplicate request must not be journaled twice");
    journal.acknowledge("request-a");
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
