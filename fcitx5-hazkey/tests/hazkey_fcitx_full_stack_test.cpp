#include <cstdlib>
#include <cstdint>
#include <ctime>
#include <functional>
#include <initializer_list>
#include <iostream>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <testfrontend_public.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/eventdispatcher.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/keysym.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputcontextmanager.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>

namespace {

[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void milestone(const char* message) {
    std::cerr << "grimodex-fcitx-full-stack: " << message << '\n';
}

std::uint32_t configuredSoakIterations() {
    const char* raw = std::getenv("GRIMODEX_FCITX_SOAK_ITERATIONS");
    if (raw == nullptr) {
        return 0;
    }
    if (*raw == '\0') {
        fail("GRIMODEX_FCITX_SOAK_ITERATIONS must be a decimal integer from 0 to 1000000");
    }

    constexpr std::uint32_t maximum = 1'000'000;
    std::uint32_t value = 0;
    for (const char* cursor = raw; *cursor != '\0'; ++cursor) {
        if (*cursor < '0' || *cursor > '9') {
            fail("GRIMODEX_FCITX_SOAK_ITERATIONS must be a decimal integer from 0 to 1000000");
        }
        const auto digit = static_cast<std::uint32_t>(*cursor - '0');
        if (value > (maximum - digit) / 10) {
            fail("GRIMODEX_FCITX_SOAK_ITERATIONS must be a decimal integer from 0 to 1000000");
        }
        value = value * 10 + digit;
    }
    return value;
}

class LiveConversionTimerScenario {
   public:
    LiveConversionTimerScenario(fcitx::Instance& instance,
                                fcitx::AddonInstance* frontend)
        : instance_(instance), frontend_(frontend) {}

    void start() {
        const auto [uuid, inputContext] = makeContext(
            "grimodex-live-conversion-timer",
            fcitx::CapabilityFlags{
                fcitx::CapabilityFlag::Preedit,
                fcitx::CapabilityFlag::SurroundingText,
            });
        uuid_ = uuid;
        inputContext_ = inputContext;

        runKeys({FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u});
        delayedReading_ = preedit();
        if (delayedReading_ != "きょう") {
            fail("live conversion did not leave the reading visible during the debounce");
        }
        if (inputContext_->inputPanel().candidateList() != nullptr) {
            fail("live conversion populated candidates before the debounce elapsed");
        }
        after(100, [this] { verifyDebounceStillPending(); });
    }

   private:
    using Step = std::function<void()>;

    std::pair<fcitx::ICUUID, fcitx::InputContext*> makeContext(
        const std::string& name, fcitx::CapabilityFlags capabilities) {
        const auto uuid =
            frontend_->call<fcitx::ITestFrontend::createInputContext>(name);
        auto* inputContext = instance_.inputContextManager().findByUUID(uuid);
        if (inputContext == nullptr) {
            fail("timer test input context was not created");
        }
        inputContext->setCapabilityFlags(capabilities);
        inputContext->focusIn();
        instance_.setCurrentInputMethod(inputContext, "grimodex", true);
        if (instance_.inputMethod(inputContext) != "grimodex") {
            fail("Grimodex input method was not activated for the timer test");
        }
        return {uuid, inputContext};
    }

    void runKeys(std::initializer_list<fcitx::KeySym> symbols) {
        runKeys(uuid_, symbols);
    }

    void runKeys(fcitx::ICUUID uuid,
                 std::initializer_list<fcitx::KeySym> symbols) {
        for (const auto symbol : symbols) {
            if (!frontend_->call<fcitx::ITestFrontend::sendKeyEvent>(
                    uuid, fcitx::Key(symbol), false)) {
                fail("Grimodex did not accept a timer-test key event");
            }
        }
    }

    std::string preedit() const {
        return preedit(inputContext_);
    }

    static std::string preedit(fcitx::InputContext* inputContext) {
        return inputContext->inputPanel().clientPreedit().toStringForCommit();
    }

    void after(uint32_t delayMilliseconds, Step step) {
        const uint64_t deadline =
            fcitx::now(CLOCK_MONOTONIC) +
            static_cast<uint64_t>(delayMilliseconds) * 1000ULL;
        timers_.push_back(instance_.eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, deadline, 1000,
            [step = std::move(step)](fcitx::EventSourceTime*, uint64_t) {
                step();
                return true;
            }));
        timers_.back()->setOneShot();
    }

    void verifyTimerFired() {
        const auto converted = preedit();
        const auto candidates = inputContext_->inputPanel().candidateList();
        if (converted.empty() || converted == delayedReading_) {
            fail("live-conversion timer did not replace the reading preedit");
        }
        if (candidates == nullptr || candidates->size() == 0) {
            fail("live-conversion timer did not refresh the candidate UI");
        }
        milestone("delayed live conversion refreshed preedit and candidate UI");

        frontend_->call<fcitx::ITestFrontend::pushCommitExpectation>(converted);
        runKeys({FcitxKey_Return});

        runKeys({FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u,
                 FcitxKey_Left});
        cancelledReading_ = preedit();
        cancelledCursor_ = inputContext_->inputPanel().clientPreedit().cursor();
        if (cancelledReading_ != "きょう") {
            fail("semantic-key cancellation unexpectedly changed the reading");
        }
        after(450, [this] { verifySemanticKeyCancelledTimer(); });
    }

    void verifyDebounceStillPending() {
        if (preedit() != delayedReading_ ||
            inputContext_->inputPanel().candidateList() != nullptr) {
            fail("live conversion fired before the configured debounce elapsed");
        }
        after(350, [this] { verifyTimerFired(); });
    }

    void verifySemanticKeyCancelledTimer() {
        if (preedit() != cancelledReading_ ||
            inputContext_->inputPanel().clientPreedit().cursor() !=
                cancelledCursor_ ||
            inputContext_->inputPanel().candidateList() != nullptr) {
            fail("a live-conversion timer survived the superseding semantic key");
        }
        milestone("semantic key cancelled pending live conversion");

        frontend_->call<fcitx::ITestFrontend::pushCommitExpectation>(
            cancelledReading_);
        runKeys({FcitxKey_Return});

        runKeys({FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u});
        inputContext_->reset();
        if (!preedit().empty()) {
            fail("Fcitx reset did not clear the delayed composition immediately");
        }
        after(450, [this] { verifyResetCancelledTimer(); });
    }

    void verifyResetCancelledTimer() {
        if (!preedit().empty() ||
            inputContext_->inputPanel().candidateList() != nullptr) {
            fail("a live-conversion timer survived Fcitx reset");
        }
        milestone("reset cancelled pending live conversion");

        runKeys({FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u});
        const auto reading = preedit();
        frontend_->call<fcitx::ITestFrontend::pushCommitExpectation>(reading);
        instance_.setCurrentInputMethod(inputContext_, "testim", true);
        if (instance_.inputMethod(inputContext_) != "testim") {
            fail("timer test could not deactivate Grimodex");
        }
        after(450, [this] { verifyDeactivateCancelledTimer(); });
    }

    void verifyDeactivateCancelledTimer() {
        if (!preedit().empty() ||
            inputContext_->inputPanel().candidateList() != nullptr) {
            fail("a live-conversion timer survived input-method deactivation");
        }
        milestone("deactivation cancelled pending live conversion");

        frontend_->call<fcitx::ITestFrontend::destroyInputContext>(uuid_);
        startIndependentContextTimers();
    }

    void startIndependentContextTimers() {
        const auto [firstUUID, firstContext] = makeContext(
            "grimodex-live-conversion-first-context",
            fcitx::CapabilityFlags{fcitx::CapabilityFlag::Preedit});
        firstUUID_ = firstUUID;
        firstContext_ = firstContext;
        runKeys(firstUUID_, {FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u,
                             FcitxKey_Left});
        firstReading_ = preedit(firstContext_);
        firstCursor_ = firstContext_->inputPanel().clientPreedit().cursor();

        const auto [secondUUID, secondContext] = makeContext(
            "grimodex-live-conversion-second-context",
            fcitx::CapabilityFlags{fcitx::CapabilityFlag::Preedit});
        secondUUID_ = secondUUID;
        secondContext_ = secondContext;
        runKeys(secondUUID_, {FcitxKey_a, FcitxKey_s, FcitxKey_h, FcitxKey_i,
                              FcitxKey_t, FcitxKey_a});
        secondReading_ = preedit(secondContext_);
        if (firstReading_ != "きょう" || secondReading_ != "あした") {
            fail("independent timer contexts did not expose their readings");
        }
        after(450, [this] { verifyIndependentContextTimers(); });
    }

    void verifyIndependentContextTimers() {
        if (preedit(firstContext_) != firstReading_ ||
            firstContext_->inputPanel().clientPreedit().cursor() !=
                firstCursor_ ||
            firstContext_->inputPanel().candidateList() != nullptr) {
            fail("cancelling one input context did not remain context-local");
        }
        if (preedit(secondContext_).empty() ||
            preedit(secondContext_) == secondReading_ ||
            secondContext_->inputPanel().candidateList() == nullptr) {
            fail("one input context cancellation suppressed another context timer");
        }
        milestone("live-conversion timers remained independent per input context");

        frontend_->call<fcitx::ITestFrontend::destroyInputContext>(secondUUID_);
        frontend_->call<fcitx::ITestFrontend::destroyInputContext>(firstUUID_);
        milestone("all delayed live-conversion scenarios passed");
        instance_.exit(0);
    }

    fcitx::Instance& instance_;
    fcitx::AddonInstance* frontend_;
    fcitx::ICUUID uuid_;
    fcitx::InputContext* inputContext_ = nullptr;
    std::string delayedReading_;
    std::string cancelledReading_;
    int cancelledCursor_ = 0;
    fcitx::ICUUID firstUUID_;
    fcitx::InputContext* firstContext_ = nullptr;
    std::string firstReading_;
    int firstCursor_ = 0;
    fcitx::ICUUID secondUUID_;
    fcitx::InputContext* secondContext_ = nullptr;
    std::string secondReading_;
    std::vector<std::unique_ptr<fcitx::EventSourceTime>> timers_;
};

}  // namespace

int main() {
    const auto soakIterations = configuredSoakIterations();
    milestone("constructing instance");
    char program[] = "grimodex-fcitx-integration";
    char disable[] = "--disable=all";
    char enable[] = "--enable=testfrontend,testui,testim,grimodex";
    char ui[] = "--ui=testui";
    char* arguments[] = {program, disable, enable, ui};
    fcitx::Instance instance(4, arguments);
    if (!instance.initialized()) {
        fail("Fcitx instance did not initialize");
    }
    milestone("instance initialized");

    // Embedded Fcitx instances do not have fcitx5's executable-owned static
    // registry. The shared loader is sufficient for the isolated test addons
    // and the product addon.
    instance.addonManager().registerDefaultLoader(nullptr);
    fcitx::EventDispatcher dispatcher;
    std::unique_ptr<LiveConversionTimerScenario> timerScenario;
    dispatcher.attach(&instance.eventLoop());
    dispatcher.schedule([&instance, &timerScenario, soakIterations] {
        milestone("scenario callback started");
        auto* frontend = instance.addonManager().addon("testfrontend", true);
        if (frontend == nullptr) {
            fail("testfrontend did not load");
        }

        const auto runKeys = [&](fcitx::ICUUID uuid,
                                 std::initializer_list<fcitx::KeySym> symbols) {
            for (const auto symbol : symbols) {
                if (!frontend->call<fcitx::ITestFrontend::sendKeyEvent>(
                        uuid, fcitx::Key(symbol), false)) {
                    fail("Grimodex did not accept an expected key event");
                }
            }
        };
        const auto makeContext = [&](const std::string& name,
                                     fcitx::CapabilityFlags capabilities) {
            const auto uuid =
                frontend->call<fcitx::ITestFrontend::createInputContext>(name);
            auto* inputContext =
                instance.inputContextManager().findByUUID(uuid);
            if (inputContext == nullptr) {
                fail("test input context was not created");
            }
            inputContext->setCapabilityFlags(capabilities);
            inputContext->focusIn();
            instance.setCurrentInputMethod(inputContext, "grimodex", true);
            if (instance.inputMethod(inputContext) != "grimodex") {
                fail("Grimodex input method was not activated");
            }
            return std::pair{uuid, inputContext};
        };

        const auto [clientPreeditUUID, clientPreeditContext] = makeContext(
            "grimodex-client-preedit",
            fcitx::CapabilityFlags{
                fcitx::CapabilityFlag::Preedit,
                fcitx::CapabilityFlag::SurroundingText,
            });
        milestone("client-preedit context activated");
        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>("かな");
        runKeys(clientPreeditUUID, {FcitxKey_k, FcitxKey_a, FcitxKey_n,
                                    FcitxKey_a, FcitxKey_Return});
        milestone("kana commit passed");

        runKeys(clientPreeditUUID, {FcitxKey_k, FcitxKey_a, FcitxKey_n,
                                    FcitxKey_a, FcitxKey_space});
        const auto initialCandidateList =
            clientPreeditContext->inputPanel().candidateList();
        if (initialCandidateList == nullptr ||
            initialCandidateList->size() <= 1) {
            fail("initial active segment did not expose candidate alternatives");
        }
        if (initialCandidateList->cursorIndex() != 0 ||
            initialCandidateList->candidate(0).text().toString().empty()) {
            fail("initial active-segment candidate was not selected");
        }
        const auto initialConversionCommit =
            clientPreeditContext->inputPanel().clientPreedit()
                .toStringForCommit();
        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>(
            initialConversionCommit);
        runKeys(clientPreeditUUID, {FcitxKey_Return});
        milestone("initial active-segment candidates passed");

        for (std::uint32_t iteration = 0; iteration < soakIterations;
             ++iteration) {
            if (!clientPreeditContext->inputPanel()
                     .clientPreedit()
                     .toStringForCommit()
                     .empty() ||
                clientPreeditContext->inputPanel().candidateList() != nullptr) {
                fail("same-session conversion soak started with stale UI state");
            }

            if (iteration % 2 == 0) {
                runKeys(clientPreeditUUID,
                        {FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u,
                         FcitxKey_space});
            } else {
                runKeys(clientPreeditUUID,
                        {FcitxKey_a, FcitxKey_s, FcitxKey_h, FcitxKey_i,
                         FcitxKey_t, FcitxKey_a, FcitxKey_space});
            }

            const auto converted = clientPreeditContext->inputPanel()
                                       .clientPreedit()
                                       .toStringForCommit();
            const auto candidates =
                clientPreeditContext->inputPanel().candidateList();
            if (converted.empty() || candidates == nullptr ||
                candidates->size() == 0) {
                fail("same-session conversion soak did not expose converted UI state");
            }
            frontend->call<fcitx::ITestFrontend::pushCommitExpectation>(
                converted);
            runKeys(clientPreeditUUID, {FcitxKey_Return});
            if (!clientPreeditContext->inputPanel()
                     .clientPreedit()
                     .toStringForCommit()
                     .empty() ||
                clientPreeditContext->inputPanel().candidateList() != nullptr) {
                fail("same-session conversion soak retained stale UI after commit");
            }
        }
        const auto soakMilestone =
            "same-session conversion soak passed: " +
            std::to_string(soakIterations) + " iterations";
        milestone(soakMilestone.c_str());

        runKeys(clientPreeditUUID,
                {FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u,
                 FcitxKey_h, FcitxKey_a, FcitxKey_i, FcitxKey_s,
                 FcitxKey_h, FcitxKey_a, FcitxKey_n, FcitxKey_i,
                 FcitxKey_i, FcitxKey_k, FcitxKey_u, FcitxKey_Left});
        const auto cursorEditedPreedit =
            clientPreeditContext->inputPanel().clientPreedit();
        const auto cursorEditedText = cursorEditedPreedit.toString();
        const auto cursorEditedCommit = cursorEditedPreedit.toStringForCommit();
        if (cursorEditedCommit.empty()) {
            fail("Left during live conversion unexpectedly lost the composition");
        }
        if (cursorEditedText.find("│") != std::string::npos) {
            fail("Left during live conversion unexpectedly entered segment editing");
        }
        if (cursorEditedPreedit.cursor() < 0 ||
            cursorEditedPreedit.cursor() >=
                static_cast<int>(cursorEditedText.size())) {
            fail("Left during live conversion did not move the character cursor");
        }
        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>(
            cursorEditedCommit);
        runKeys(clientPreeditUUID, {FcitxKey_Return});
        milestone("live-conversion Left kept character cursor editing");

        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>("カナ");
        runKeys(clientPreeditUUID, {FcitxKey_k, FcitxKey_a, FcitxKey_n,
                                    FcitxKey_a, FcitxKey_F7,
                                    FcitxKey_Return});
        milestone("F7 transform passed");

        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>("カナ");
        runKeys(clientPreeditUUID, {FcitxKey_k, FcitxKey_a, FcitxKey_n,
                                    FcitxKey_a, FcitxKey_Katakana,
                                    FcitxKey_Return});
        milestone("JIS mode-key transform passed");

        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>("かな");
        runKeys(clientPreeditUUID,
                {FcitxKey_k, FcitxKey_a, FcitxKey_n, FcitxKey_a});
        instance.setCurrentInputMethod(clientPreeditContext, "testim", true);
        milestone("deactivation commit passed");

        const auto [panelPreeditUUID, panelPreeditContext] = makeContext(
            "grimodex-panel-preedit", fcitx::CapabilityFlags{});
        milestone("panel-preedit context activated");
        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>("イ");
        runKeys(panelPreeditUUID, {FcitxKey_kana_A, FcitxKey_kana_I,
                                   FcitxKey_Home, FcitxKey_Delete,
                                   FcitxKey_Return});
        milestone("direct-kana cursor/edit passed");

        frontend->call<fcitx::ITestFrontend::destroyInputContext>(
            panelPreeditUUID);
        frontend->call<fcitx::ITestFrontend::destroyInputContext>(
            clientPreeditUUID);
        (void)panelPreeditContext;
        milestone("synchronous scenarios passed");
        timerScenario =
            std::make_unique<LiveConversionTimerScenario>(instance, frontend);
        timerScenario->start();
    });
    const int result = instance.exec();
    dispatcher.detach();
    return result;
}
