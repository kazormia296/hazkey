#ifndef _FCITX5_HAZKEY_HAZKEY_STATE_H_
#define _FCITX5_HAZKEY_HAZKEY_STATE_H_

#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/surroundingtext.h>
#include <fcitx-utils/event.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <string>

#include "base.pb.h"
#include "hazkey_action_mapper.h"
#include "hazkey_candidate.h"
#include "hazkey_server_connector.h"

namespace fcitx {

class HazkeyEngine;

/// Per-InputContext Protocol-v2 adapter.
///
/// Semantic composition state is owned by Swift's CompositionSession. This
/// class maps Fcitx key/lifecycle events to actions, renders snapshots, and
/// applies idempotent client effects; it deliberately has no parallel IME
/// phase flags or local preedit model.
class HazkeyState : public InputContextProperty {
   public:
    HazkeyState(HazkeyEngine* engine, InputContext* ic);
    ~HazkeyState() override;

    void commitPreedit();
    void keyEvent(KeyEvent& keyEvent);
    void capabilityAboutToChange(CapabilityFlags newFlags);
    void reset();

   private:
    bool isInputableEvent(const KeyEvent& keyEvent) const;
    bool isAltDigitKeyEvent(const KeyEvent& keyEvent) const;
    void discardLocalComposition();
    void keyEventV2(KeyEvent& keyEvent);
    bool dispatchV2(const HazkeySemanticAction& action,
                    const std::string& insertedText = "");
    bool applyV2Response(
        const std::optional<hazkey::ResponseEnvelope>& response);
    void renderV2Snapshot();
    void selectV2Candidate(int index);
    void forgetV2Candidate(int index);
    bool reconvertV2Selection();
    void updateSurroundingTextV2();
    bool resolvePendingLearning(bool commit);
    bool flushDeferredActions(bool tryConnect = false,
                              bool requireSuccess = true);
    void scheduleLiveConversion(uint64_t effectID, uint32_t delayMs,
                                uint64_t scheduledRevision);
    void cancelLiveConversionTimer();
    void applyDelayedLiveConversion(uint64_t effectID,
                                    uint64_t scheduledRevision);

    HazkeyEngine* engine_;
    InputContext* ic_;
    HazkeyServerSession server_;
    bool protocolAvailable_ = false;
    hazkey::SessionSnapshot snapshot_;
    std::unique_ptr<EventSourceTime> liveConversionTimer_;
    uint64_t pendingLiveConversionEffectID_ = 0;
};

}  // namespace fcitx

#endif  // _FCITX5_HAZKEY_HAZKEY_STATE_H_
