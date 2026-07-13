#ifndef FCITX5_HAZKEY_HAZKEY_EFFECT_LEDGER_H_
#define FCITX5_HAZKEY_HAZKEY_EFFECT_LEDGER_H_

#include <cstddef>
#include <cstdint>

/// Client-side idempotency guard for commit/delete/switch effects.  The
/// server may replay a snapshot after a response loss; the effect ID, rather
/// than the response instance, is the unit of application.
class HazkeyEffectLedger {
   public:
    explicit HazkeyEffectLedger(std::size_t = 256) {}

    bool claim(uint64_t effectID);
    void clear();
    std::size_t size() const { return appliedCount_; }

   private:
    uint64_t highestApplied_ = 0;
    std::size_t appliedCount_ = 0;
};

#endif  // FCITX5_HAZKEY_HAZKEY_EFFECT_LEDGER_H_
