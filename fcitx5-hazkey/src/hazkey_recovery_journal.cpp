#include "hazkey_recovery_journal.h"

#include <algorithm>

void HazkeyRecoveryJournal::record(HazkeyJournalEntry entry) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == entry.requestID;
        });
    if (existing != pending_.end()) {
        return;
    }
    if (pending_.size() >= limit_) {
        pending_.erase(pending_.begin());
    }
    pending_.push_back(std::move(entry));
}

void HazkeyRecoveryJournal::acknowledge(const std::string& requestID) {
    pending_.erase(
        std::remove_if(
            pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
                return value.requestID == requestID;
            }),
        pending_.end());
}

void HazkeyRecoveryJournal::confirmSnapshot(std::string serializedSnapshot) {
    lastSnapshot_ = std::move(serializedSnapshot);
}

void HazkeyRecoveryJournal::clear() {
    lastSnapshot_.clear();
    pending_.clear();
}
