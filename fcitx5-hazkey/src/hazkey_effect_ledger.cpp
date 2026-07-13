#include "hazkey_effect_ledger.h"

bool HazkeyEffectLedger::claim(uint64_t effectID) {
    // Effect IDs are monotonic within one restored session namespace. A
    // high-water mark is bounded and cannot forget a recently applied effect
    // the way an evicting set can.
    if (effectID == 0 || effectID <= highestApplied_) {
        return false;
    }
    highestApplied_ = effectID;
    ++appliedCount_;
    return true;
}

void HazkeyEffectLedger::clear() {
    highestApplied_ = 0;
    appliedCount_ = 0;
}
