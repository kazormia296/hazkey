#include "controllers/user_interface_tab_controller.h"

#include <algorithm>
#include <cstdint>

#include "config_definitions.h"
#include "config_macros.h"
#include "controllers/direct_commit_targets.h"
#include "ui_mainwindow.h"

namespace hazkey::settings {

UserInterfaceTabController::UserInterfaceTabController(Ui::MainWindow* ui,
                                                       QObject* parent)
    : QObject(parent), ui_(ui) {}

void UserInterfaceTabController::setContext(const TabContext& context) {
    context_ = context;
}

void UserInterfaceTabController::loadFromConfig() {
    if (!context_.currentProfile) return;

    SET_COMBO_FROM_CONFIG(ConfigDefs::AutoConvertMode, ui_->autoConvertion,
                          context_.currentProfile->auto_convert_mode());
    SET_COMBO_FROM_CONFIG(ConfigDefs::AuxTextMode, ui_->auxiliaryText,
                          context_.currentProfile->aux_text_mode());
    SET_COMBO_FROM_CONFIG(ConfigDefs::SuggestionListMode, ui_->suggestionList,
                          context_.currentProfile->suggestion_list_mode());

    SET_SPINBOX(
        ui_->liveConversionDelay,
        context_.currentProfile->has_live_conversion_delay_msec()
            ? static_cast<int>(std::min<uint32_t>(
                  context_.currentProfile->live_conversion_delay_msec(), 1000))
            : ConfigDefs::SpinboxDefaults::LIVE_CONVERSION_DELAY_MSEC,
        ConfigDefs::SpinboxDefaults::LIVE_CONVERSION_DELAY_MSEC);
    SET_SPINBOX(ui_->numSuggestion, context_.currentProfile->num_suggestions(),
                ConfigDefs::SpinboxDefaults::NUM_SUGGESTIONS);
    SET_SPINBOX(ui_->numCandidatesPerPage,
                context_.currentProfile->num_candidates_per_page(),
                ConfigDefs::SpinboxDefaults::NUM_CANDIDATES_PER_PAGE);
    ui_->directCommitPunctuation->setChecked(
        context_.currentProfile->has_direct_commit_targets()
        && hasPunctuationDirectCommitTarget(
            context_.currentProfile->direct_commit_targets()));
}

void UserInterfaceTabController::saveToConfig() {
    if (!context_.currentProfile) return;

    context_.currentProfile->set_auto_convert_mode(
        GET_COMBO_TO_CONFIG(ConfigDefs::AutoConvertMode, ui_->autoConvertion));
    context_.currentProfile->set_aux_text_mode(
        GET_COMBO_TO_CONFIG(ConfigDefs::AuxTextMode, ui_->auxiliaryText));
    context_.currentProfile->set_suggestion_list_mode(GET_COMBO_TO_CONFIG(
        ConfigDefs::SuggestionListMode, ui_->suggestionList));

    context_.currentProfile->set_live_conversion_delay_msec(
        static_cast<uint32_t>(GET_SPINBOX_INT(ui_->liveConversionDelay)));
    context_.currentProfile->set_num_suggestions(
        GET_SPINBOX_INT(ui_->numSuggestion));
    context_.currentProfile->set_num_candidates_per_page(
        GET_SPINBOX_INT(ui_->numCandidatesPerPage));
    const uint32_t directCommitTargets =
        context_.currentProfile->has_direct_commit_targets()
            ? context_.currentProfile->direct_commit_targets()
            : 0;
    context_.currentProfile->set_direct_commit_targets(
        withPunctuationDirectCommitEnabled(
            directCommitTargets,
            ui_->directCommitPunctuation->isChecked()));
}

}  // namespace hazkey::settings
