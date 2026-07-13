#ifndef HAZKEY_SETTINGS_CONTROLLERS_DICTIONARY_TAB_CONTROLLER_H_
#define HAZKEY_SETTINGS_CONTROLLERS_DICTIONARY_TAB_CONTROLLER_H_

#include <QObject>
#include <QModelIndex>

#include <vector>

#include "controllers/tab_context.h"

QT_BEGIN_NAMESPACE
class QStandardItemModel;
namespace Ui {
class MainWindow;
}
QT_END_NAMESPACE

namespace hazkey::settings {

class DictionaryTabController : public QObject {
    Q_OBJECT

   public:
    explicit DictionaryTabController(Ui::MainWindow* ui, QObject* parent);
    void setContext(const TabContext& context);
    void connectSignals();
    void loadFromConfig();
    void saveToConfig();

   private slots:
    void addEntry();
    void editEntry(const QModelIndex& index);
    void deleteEntry();
    void importEntries();
    void exportEntries();

   private:
    void refreshEntries();
    void rebuildModel();
    void showOperationError(const QString& operation);

    Ui::MainWindow* ui_;
    TabContext context_;
    QStandardItemModel* model_;
    std::vector<hazkey::config::UserDictionaryEntry> entries_;
};

}  // namespace hazkey::settings

#endif  // HAZKEY_SETTINGS_CONTROLLERS_DICTIONARY_TAB_CONTROLLER_H_
