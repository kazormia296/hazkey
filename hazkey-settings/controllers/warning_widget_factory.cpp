#include "controllers/warning_widget_factory.h"

#include <QColor>
#include <QHBoxLayout>
#include <QLabel>
#include <QPalette>
#include <QPushButton>

namespace hazkey::settings {

QWidget* WarningWidgetFactory::create(const QString& message,
                                      const QString& backgroundColor,
                                      const QString& buttonText,
                                      std::function<void()> buttonCallback,
                                      const QString& secondaryButtonText,
                                      std::function<void()> secondaryButtonCallback) {
    const QColor background(backgroundColor);
    const QColor foreground =
        !background.isValid() || background.lightnessF() > 0.55
            ? QColor("#171717")
            : QColor("#f5f5f5");

    QWidget* warningWidget = new QWidget();
    warningWidget->setStyleSheet(
        QString("background-color: %1; color: %2; padding: 5px;")
            .arg(backgroundColor, foreground.name()));
    QHBoxLayout* warningLayout = new QHBoxLayout(warningWidget);

    QLabel* warningLabel = new QLabel(message);
    warningLabel->setWordWrap(true);
    QPalette warningPalette = warningLabel->palette();
    warningPalette.setColor(QPalette::WindowText, foreground);
    warningPalette.setColor(QPalette::Text, foreground);
    warningLabel->setPalette(warningPalette);
    warningLabel->setStyleSheet(
        QString("QLabel { color: %1; }").arg(foreground.name()));
    warningLayout->addWidget(warningLabel);

    if (!buttonText.isEmpty() && buttonCallback) {
        QPushButton* button = new QPushButton(buttonText);
        QObject::connect(button, &QPushButton::clicked, warningWidget,
                         [buttonCallback]() { buttonCallback(); });
        warningLayout->addWidget(button);
    }

    if (!secondaryButtonText.isEmpty() && secondaryButtonCallback) {
        QPushButton* button = new QPushButton(secondaryButtonText);
        QObject::connect(button, &QPushButton::clicked, warningWidget,
                         [secondaryButtonCallback]() {
                             secondaryButtonCallback();
                         });
        warningLayout->addWidget(button);
    }

    return warningWidget;
}

}  // namespace hazkey::settings
