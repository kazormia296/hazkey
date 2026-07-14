#ifndef FCITX5_HAZKEY_HAZKEY_RECOVERY_JOURNAL_H_
#define FCITX5_HAZKEY_HAZKEY_RECOVERY_JOURNAL_H_

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

struct HazkeyJournalEntry {
    std::string requestID;
    std::string serializedAction;
    uint64_t expectedRevision = 0;
    std::string sessionID;
    // Once an entry has touched the wire, its request ID, action, and expected
    // revision form one immutable idempotency key. Only never-sent entries may
    // be rebased behind an earlier replay.
    bool sent = false;
};

/// In-memory journal used by the addon while a session is reconnecting.  It
/// intentionally has no disk persistence API; callers must never persist a
/// secure-input composition.
class HazkeyRecoveryJournal {
   public:
    explicit HazkeyRecoveryJournal(std::size_t limit = 64) : limit_(limit) {}

    // The journal is fail-closed: capacity pressure must never evict an older
    // semantic action and invert the order observed by the application.
    bool record(HazkeyJournalEntry entry);
    bool replace(const std::string& requestID, HazkeyJournalEntry entry);
    bool markSent(const std::string& requestID);
    bool rebaseUnsent(const std::string& requestID,
                      std::string serializedAction,
                      uint64_t expectedRevision,
                      std::string sessionID);
    bool rebindSent(const std::string& requestID,
                    std::string serializedAction,
                    uint64_t expectedRevision,
                    std::string sessionID);
    void acknowledge(const std::string& requestID);
    void confirmSnapshot(std::string serializedSnapshot);
    void clear();

    const std::string& lastSnapshot() const { return lastSnapshot_; }
    const std::vector<HazkeyJournalEntry>& pending() const { return pending_; }

   private:
    std::size_t limit_;
    std::string lastSnapshot_;
    std::vector<HazkeyJournalEntry> pending_;
};

#endif  // FCITX5_HAZKEY_HAZKEY_RECOVERY_JOURNAL_H_
