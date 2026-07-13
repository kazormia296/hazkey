#include "controllers/dictionary_tab_controller.h"

#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFile>
#include <QFileDialog>
#include <QFormLayout>
#include <QHeaderView>
#include <QLineEdit>
#include <QMessageBox>
#include <QPushButton>
#include <QSaveFile>
#include <QStandardItemModel>
#include <QTableView>

#include <optional>

#include "ui_mainwindow.h"

namespace hazkey::settings {
namespace {

std::optional<hazkey::config::UserDictionaryEntry> editEntryDialog(
    QWidget* parent,
    const std::optional<hazkey::config::UserDictionaryEntry>& existing) {
    QDialog dialog(parent);
    dialog.setWindowTitle(existing.has_value()
                              ? QObject::tr("Edit dictionary entry")
                              : QObject::tr("New dictionary entry"));
    auto* form = new QFormLayout(&dialog);
    auto* reading = new QLineEdit(&dialog);
    auto* surface = new QLineEdit(&dialog);
    auto* partOfSpeech = new QComboBox(&dialog);
    partOfSpeech->addItem(QObject::tr("Common noun"), "noun");
    partOfSpeech->addItem(QObject::tr("Proper noun"), "proper_noun");
    partOfSpeech->addItem(QObject::tr("Person name"), "person");
    partOfSpeech->addItem(QObject::tr("Family name"), "surname");
    partOfSpeech->addItem(QObject::tr("Given name"), "given_name");
    partOfSpeech->addItem(QObject::tr("Place name"), "place");
    partOfSpeech->addItem(QObject::tr("Organization"), "organization");
    form->addRow(QObject::tr("Reading"), reading);
    form->addRow(QObject::tr("Word"), surface);
    form->addRow(QObject::tr("Part of speech"), partOfSpeech);

    if (existing.has_value()) {
        reading->setText(QString::fromStdString(existing->reading()));
        surface->setText(QString::fromStdString(existing->surface()));
        const int index = partOfSpeech->findData(
            QString::fromStdString(existing->part_of_speech()));
        if (index >= 0) {
            partOfSpeech->setCurrentIndex(index);
        }
    }

    auto* buttons = new QDialogButtonBox(
        QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
    QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog,
                     &QDialog::accept);
    QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog,
                     &QDialog::reject);
    form->addRow(buttons);

    if (dialog.exec() != QDialog::Accepted) {
        return std::nullopt;
    }
    if (reading->text().trimmed().isEmpty() ||
        surface->text().trimmed().isEmpty()) {
        QMessageBox::warning(parent, QObject::tr("Invalid entry"),
                             QObject::tr("Reading and word are required."));
        return std::nullopt;
    }

    hazkey::config::UserDictionaryEntry result;
    if (existing.has_value()) {
        result = *existing;
    } else {
        result.set_layer(hazkey::config::PERSONAL);
    }
    result.set_reading(reading->text().trimmed().toStdString());
    result.set_surface(surface->text().trimmed().toStdString());
    result.set_part_of_speech(
        partOfSpeech->currentData().toString().toStdString());
    return result;
}

QString layerName(hazkey::config::UserDictionaryLayer layer) {
    switch (layer) {
        case hazkey::config::SYSTEM:
            return QObject::tr("System");
        case hazkey::config::PROJECT:
            return QObject::tr("Project");
        case hazkey::config::TEMPORARY:
            return QObject::tr("Temporary");
        case hazkey::config::PERSONAL:
        case hazkey::config::USER_DICTIONARY_LAYER_UNSPECIFIED:
        default:
            return QObject::tr("Personal");
    }
}

}  // namespace

DictionaryTabController::DictionaryTabController(Ui::MainWindow* ui,
                                                 QObject* parent)
    : QObject(parent),
      ui_(ui),
      context_(),
      model_(new QStandardItemModel(this)) {
    ui_->userDictViewer->setModel(model_);
    ui_->userDictViewer->setSelectionBehavior(QAbstractItemView::SelectRows);
    ui_->userDictViewer->setSelectionMode(QAbstractItemView::SingleSelection);
    ui_->userDictViewer->setEditTriggers(QAbstractItemView::NoEditTriggers);
    ui_->userDictViewer->horizontalHeader()->setStretchLastSection(true);
    ui_->useUserDict->setChecked(true);
    ui_->useUserDict->setEnabled(false);
    ui_->useUserDict->setToolTip(
        tr("User dictionary entries are always enabled."));
}

void DictionaryTabController::setContext(const TabContext& context) {
    context_ = context;
}

void DictionaryTabController::connectSignals() {
    connect(ui_->userDictNewEntry, &QPushButton::clicked, this,
            &DictionaryTabController::addEntry);
    connect(ui_->userDictDeleteEntry, &QPushButton::clicked, this,
            &DictionaryTabController::deleteEntry);
    connect(ui_->userDictImport, &QPushButton::clicked, this,
            &DictionaryTabController::importEntries);
    connect(ui_->userDictExport, &QPushButton::clicked, this,
            &DictionaryTabController::exportEntries);
    connect(ui_->userDictViewer, &QTableView::doubleClicked, this,
            &DictionaryTabController::editEntry);
}

void DictionaryTabController::loadFromConfig() { refreshEntries(); }

void DictionaryTabController::saveToConfig() {
    // Dictionary mutations are applied atomically when each operation is
    // confirmed, independently of profile configuration.
}

void DictionaryTabController::refreshEntries() {
    if (context_.server == nullptr) {
        entries_.clear();
        rebuildModel();
        return;
    }
    const auto entries = context_.server->listUserDictionary();
    if (!entries.has_value()) {
        showOperationError(tr("load"));
        return;
    }
    entries_ = *entries;
    rebuildModel();
}

void DictionaryTabController::rebuildModel() {
    model_->clear();
    model_->setHorizontalHeaderLabels(
        {tr("Reading"), tr("Word"), tr("Part of speech"), tr("Layer")});
    for (const auto& entry : entries_) {
        QList<QStandardItem*> row;
        row << new QStandardItem(QString::fromStdString(entry.reading()))
            << new QStandardItem(QString::fromStdString(entry.surface()))
            << new QStandardItem(
                   QString::fromStdString(entry.part_of_speech()))
            << new QStandardItem(layerName(entry.layer()));
        model_->appendRow(row);
    }
    ui_->userDictViewer->resizeColumnsToContents();
    ui_->userDictDeleteEntry->setEnabled(!entries_.empty());
}

void DictionaryTabController::addEntry() {
    if (context_.server == nullptr) return;
    const auto entry = editEntryDialog(ui_->dictionaryTab, std::nullopt);
    if (!entry.has_value()) return;
    if (!context_.server->addUserDictionaryEntry(*entry)) {
        showOperationError(tr("add"));
        return;
    }
    refreshEntries();
}

void DictionaryTabController::editEntry(const QModelIndex& index) {
    if (context_.server == nullptr || index.row() < 0 ||
        static_cast<std::size_t>(index.row()) >= entries_.size()) {
        return;
    }
    const auto entry = editEntryDialog(
        ui_->dictionaryTab, entries_[static_cast<std::size_t>(index.row())]);
    if (!entry.has_value()) return;
    if (!context_.server->updateUserDictionaryEntry(*entry)) {
        showOperationError(tr("update"));
        return;
    }
    refreshEntries();
}

void DictionaryTabController::deleteEntry() {
    if (context_.server == nullptr) return;
    const QModelIndex index = ui_->userDictViewer->currentIndex();
    if (!index.isValid() || index.row() < 0 ||
        static_cast<std::size_t>(index.row()) >= entries_.size()) {
        return;
    }
    const auto& entry = entries_[static_cast<std::size_t>(index.row())];
    if (QMessageBox::question(
            ui_->dictionaryTab, tr("Delete dictionary entry"),
            tr("Delete “%1”? ").arg(QString::fromStdString(entry.surface())),
            QMessageBox::Yes | QMessageBox::No) != QMessageBox::Yes) {
        return;
    }
    if (!context_.server->removeUserDictionaryEntry(entry.id())) {
        showOperationError(tr("delete"));
        return;
    }
    refreshEntries();
}

void DictionaryTabController::importEntries() {
    if (context_.server == nullptr) return;
    const QString path = QFileDialog::getOpenFileName(
        ui_->dictionaryTab, tr("Import user dictionary"), QString(),
        tr("JSON files (*.json);;All files (*)"));
    if (path.isEmpty()) return;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        showOperationError(tr("read import file"));
        return;
    }
    const auto choice = QMessageBox::question(
        ui_->dictionaryTab, tr("Import mode"),
        tr("Merge entries with the current dictionary? Choose No to replace "
           "the current dictionary."),
        QMessageBox::Yes | QMessageBox::No | QMessageBox::Cancel);
    if (choice == QMessageBox::Cancel) return;
    if (!context_.server->importUserDictionary(
            file.readAll().toStdString(), choice == QMessageBox::Yes)) {
        showOperationError(tr("import"));
        return;
    }
    refreshEntries();
}

void DictionaryTabController::exportEntries() {
    if (context_.server == nullptr) return;
    const auto json = context_.server->exportUserDictionary();
    if (!json.has_value()) {
        showOperationError(tr("export"));
        return;
    }
    const QString path = QFileDialog::getSaveFileName(
        ui_->dictionaryTab, tr("Export user dictionary"),
        QStringLiteral("grimodex-user-dictionary.json"),
        tr("JSON files (*.json);;All files (*)"));
    if (path.isEmpty()) return;
    QSaveFile file(path);
    const QByteArray data = QByteArray::fromStdString(*json);
    if (!file.open(QIODevice::WriteOnly) || file.write(data) != data.size() ||
        !file.commit()) {
        showOperationError(tr("write export file"));
    }
}

void DictionaryTabController::showOperationError(const QString& operation) {
    QMessageBox::critical(
        ui_->dictionaryTab, tr("User dictionary error"),
        tr("Failed to %1 the user dictionary. Check the Grimodex IME "
           "service and the entry values.")
            .arg(operation));
}

}  // namespace hazkey::settings
