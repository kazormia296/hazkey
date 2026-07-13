#ifndef HAZKEY_SETTINGS_CONTROLLERS_AI_TAB_CONTROLLER_H_
#define HAZKEY_SETTINGS_CONTROLLERS_AI_TAB_CONTROLLER_H_

#include <QObject>
#include <QString>
#include <atomic>

#include "controllers/tab_context.h"

class QWidget;
class QLabel;
class QPushButton;

namespace Ui {
class MainWindow;
}

namespace hazkey::settings {

class AiTabController : public QObject {
    Q_OBJECT

   public:
    AiTabController(Ui::MainWindow* ui, QWidget* window, QObject* parent);
    void setContext(const TabContext& context);
    void connectSignals();
    void loadFromConfig();
    void saveToConfig();

   private slots:
    void onDownloadZenzaiModel();
    void onSelectLocalZenzaiModel();
    void onRefreshZenzaiRuntimeDiagnostics();

   private:
    QString calculateFileSHA256(const QString& filePath);
    void refreshWarnings();
    void populateDeviceList();
    void populateGrimodexScopeList();
    void updateGrimodexScopeFromProfile();
    void refreshZenzaiRuntimeDiagnostics();
    QString zenzaiRuntimeStatusText(
        ::hazkey::config::ZenzaiRuntimeDiagnostics_Status status) const;
    void refreshGrimodexDiagnostics();
    QString grimodexScopeReasonText(
        ::hazkey::config::GrimodexDiagnostics_ScopeReason reason) const;
    void updateSelectionFromProfile();
    QString managedZenzaiModelPath() const;

    Ui::MainWindow* ui_;
    QWidget* window_;
    QLabel* zenzaiDiagnosticsLabel_;
    QPushButton* zenzaiDiagnosticsRefreshButton_;
    QLabel* grimodexDiagnosticsLabel_;
    TabContext context_;
    std::atomic<bool> isLoading_{false};
};

}  // namespace hazkey::settings

#endif  // HAZKEY_SETTINGS_CONTROLLERS_AI_TAB_CONTROLLER_H_
