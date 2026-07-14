#include "hazkey_recovery_journal.h"

#include <algorithm>

bool HazkeyRecoveryJournal::record(HazkeyJournalEntry entry) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == entry.requestID;
        });
    if (existing != pending_.end()) {
        // A request ID is an immutable server-cache key. Treat only the exact
        // same wire binding as an idempotent duplicate; silently accepting a
        // different payload/revision/session would let the caller send an
        // envelope the recovery journal cannot reproduce.
        return existing->serializedAction == entry.serializedAction &&
               existing->expectedRevision == entry.expectedRevision &&
               existing->sessionID == entry.sessionID;
    }
    if (pending_.size() >= limit_) {
        return false;
    }
    pending_.push_back(std::move(entry));
    return true;
}

bool HazkeyRecoveryJournal::replace(const std::string& requestID,
                                    HazkeyJournalEntry entry) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == requestID;
        });
    if (existing == pending_.end()) {
        return false;
    }
    const auto duplicate = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == entry.requestID && &value != &*existing;
        });
    if (duplicate != pending_.end()) {
        return false;
    }
    *existing = std::move(entry);
    return true;
}

bool HazkeyRecoveryJournal::markSent(const std::string& requestID) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == requestID;
        });
    if (existing == pending_.end()) {
        return false;
    }
    existing->sent = true;
    return true;
}

bool HazkeyRecoveryJournal::rebaseUnsent(
    const std::string& requestID, std::string serializedAction,
    uint64_t expectedRevision, std::string sessionID) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == requestID;
        });
    if (existing == pending_.end() || existing->sent) {
        return false;
    }
    existing->serializedAction = std::move(serializedAction);
    existing->expectedRevision = expectedRevision;
    existing->sessionID = std::move(sessionID);
    return true;
}

bool HazkeyRecoveryJournal::rebindSent(
    const std::string& requestID, std::string serializedAction,
    uint64_t expectedRevision, std::string sessionID) {
    auto existing = std::find_if(
        pending_.begin(), pending_.end(), [&](const HazkeyJournalEntry& value) {
            return value.requestID == requestID;
        });
    if (existing == pending_.end() || !existing->sent) {
        return false;
    }
    existing->serializedAction = std::move(serializedAction);
    existing->expectedRevision = expectedRevision;
    existing->sessionID = std::move(sessionID);
    return true;
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
