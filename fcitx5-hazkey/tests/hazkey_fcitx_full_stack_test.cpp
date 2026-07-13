#include <cstdlib>
#include <initializer_list>
#include <iostream>
#include <string>

#include <testfrontend_public.h>
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

}  // namespace

int main() {
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
    dispatcher.attach(&instance.eventLoop());
    dispatcher.schedule([&instance] {
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

        runKeys(clientPreeditUUID,
                {FcitxKey_k, FcitxKey_y, FcitxKey_o, FcitxKey_u,
                 FcitxKey_h, FcitxKey_a, FcitxKey_i, FcitxKey_s,
                 FcitxKey_h, FcitxKey_a, FcitxKey_n, FcitxKey_i,
                 FcitxKey_i, FcitxKey_k, FcitxKey_u, FcitxKey_Left});
        const auto segmentedPreedit =
            clientPreeditContext->inputPanel().clientPreedit();
        const auto segmentedCommit = segmentedPreedit.toStringForCommit();
        if (segmentedCommit.empty()) {
            fail("Left during live conversion unexpectedly lost the composition");
        }
        frontend->call<fcitx::ITestFrontend::pushCommitExpectation>(
            segmentedCommit);
        runKeys(clientPreeditUUID, {FcitxKey_Return});
        milestone("live-conversion Left preserved composition and committed");

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
        milestone("all scenarios passed");
        instance.exit(0);
    });
    const int result = instance.exec();
    dispatcher.detach();
    return result;
}
