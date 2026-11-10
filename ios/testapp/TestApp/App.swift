// droidctl's iOS test app (IOS-HANDOFF.md §7.2): every input path logs a DTA event (os_log +
// Documents/dta.log) so the e2e tests judge droidctl by the app's own record, never by droidctl's
// output. Tab 1 ("Main") is the S1 research app's screen unchanged (research/ios-spike/TestApp),
// so the I2 tests and fixtures carry over; tab 2 ("Forms") adds what S1 lacked: number pads with
// and without a Done bar, a secure field, a masked (formatted) field, a splash → home transition.
import SwiftUI
import UIKit
import UserNotifications
import WebKit
import os

let dtaLog = Logger(subsystem: "dev.droidctl.testapp", category: "DTA")
let dtaFile: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("dta.log")
let iso: ISO8601DateFormatter = { let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f }()

func dta(_ event: String) {
    let line = "\(iso.string(from: Date())) DTA \(event)\n"
    dtaLog.log("DTA \(event, privacy: .public)")
    if let h = try? FileHandle(forWritingTo: dtaFile) { h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close() }
    else { try? line.write(to: dtaFile, atomically: true, encoding: .utf8) }
    DispatchQueue.main.async { status?.text = event }
}
weak var status: UILabel?

final class LoggingButton: UIButton {
    override func touchesBegan(_ t: Set<UITouch>, with e: UIEvent?) { dta("\(accessibilityIdentifier ?? "button") touch_began"); super.touchesBegan(t, with: e) }
    override func accessibilityActivate() -> Bool { dta("\(accessibilityIdentifier ?? "button") ax_activate"); return super.accessibilityActivate() }
}

/// Not a UIControl: only accessibilityActivate or a real touch can trigger it.
final class CustomView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        isAccessibilityElement = true; accessibilityLabel = "Custom"; accessibilityIdentifier = "dta_custom"
        accessibilityTraits = .button; backgroundColor = .systemTeal
    }
    required init?(coder: NSCoder) { fatalError() }
    override func accessibilityActivate() -> Bool { dta("custom ax_activate"); return true }
    override func touchesEnded(_ t: Set<UITouch>, with e: UIEvent?) { dta("custom touch_ended") }
}

struct SUIControls: View {
    @State var on = false
    var body: some View {
        HStack {
            Button("SwiftUI Button") { dta("sui_button action") }.accessibilityIdentifier("dta_sui_button")
            Toggle("SwiftUI Toggle", isOn: $on).accessibilityIdentifier("dta_sui_toggle")
                .onChange(of: on) { v in dta("sui_toggle value=\(v ? 1 : 0)") }
        }
    }
}

func button(_ title: String, _ id: String, _ action: @escaping () -> Void) -> UIButton {
    let b = LoggingButton(type: .system); b.setTitle(title, for: .normal); b.accessibilityIdentifier = id
    b.addAction(UIAction { _ in dta("\(id) action"); action() }, for: .primaryActionTriggered)
    return b
}

/// Pinch target: logs the final scale of each pinch (multi-touch ground truth).
final class PinchView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .systemOrange; isAccessibilityElement = true; accessibilityLabel = "Pinch"; accessibilityIdentifier = "dta_pinch"
        addGestureRecognizer(UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:))))
    }
    required init?(coder: NSCoder) { fatalError() }
    @objc func pinched(_ g: UIPinchGestureRecognizer) {
        if g.state == .began { dta("pinch began touches=\(g.numberOfTouches)") }
        if g.state == .ended { dta("pinch ended scale=\(String(format: "%.2f", g.scale))") }
    }
}

final class WebHandler: NSObject, WKScriptMessageHandler {
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { dta("web \(m.body)") }
}
let webHandler = WebHandler()
let webHTML = """
<html><head><meta name=viewport content="width=device-width"></head><body style="font:16px -apple-system">
<h3>Web section</h3>
<button id=wb onclick="webkit.messageHandlers.dta.postMessage('button click')">Web Button</button>
<input id=wi placeholder="Web input" oninput="webkit.messageHandlers.dta.postMessage('input value='+this.value)">
<a href="#x" onclick="webkit.messageHandlers.dta.postMessage('link click')">Web Link</a>
<label><input type=checkbox onchange="webkit.messageHandlers.dta.postMessage('checkbox '+this.checked)"> Web Checkbox</label>
</body></html>
"""

final class MainVC: UIViewController, UITableViewDataSource, UITableViewDelegate, UITextFieldDelegate {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let lbl = UILabel(); lbl.text = "ready"; lbl.accessibilityIdentifier = "dta_status"; status = lbl

        let btn = button("Press me", "dta_button") {}
        let custom = CustomView()
        let sw = UISwitch(); sw.accessibilityIdentifier = "dta_switch"; sw.accessibilityLabel = "Switch"
        sw.addAction(UIAction { _ in dta("switch value=\(sw.isOn ? 1 : 0)") }, for: .valueChanged)
        let sl = UISlider(); sl.accessibilityIdentifier = "dta_slider"; sl.accessibilityLabel = "Slider"; sl.value = 0.5
        sl.addAction(UIAction { _ in dta("slider value=\(String(format: "%.2f", sl.value))") }, for: .valueChanged)
        let row1 = UIStackView(arrangedSubviews: [btn, sw, sl]); row1.spacing = 16

        let field = UITextField(); field.borderStyle = .roundedRect; field.placeholder = "Type here"; field.accessibilityIdentifier = "dta_field"
        field.autocorrectionType = .no; field.delegate = self
        field.addAction(UIAction { _ in dta("field text=\(field.text ?? "")") }, for: .editingChanged)
        field.addAction(UIAction { _ in dta("field editing_began") }, for: .editingDidBegin)

        let alert = button("Alert", "dta_alert") { [weak self] in
            let a = UIAlertController(title: "Test alert", message: "Pick one", preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "OK", style: .default) { _ in dta("alert ok") })
            a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in dta("alert cancel") })
            self?.present(a, animated: true)
        }
        let perm = button("Permission", "dta_perm") {
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { granted, _ in dta("perm granted=\(granted ? 1 : 0)") }
        }
        let row2 = UIStackView(arrangedSubviews: [alert, perm]); row2.spacing = 24

        let host = UIHostingController(rootView: SUIControls()); addChild(host); host.didMove(toParent: self)

        let pinch = PinchView()
        let cfg = WKWebViewConfiguration(); cfg.userContentController.add(webHandler, name: "dta")
        let web = WKWebView(frame: .zero, configuration: cfg); web.accessibilityIdentifier = "dta_web"
        web.loadHTMLString(webHTML, baseURL: nil)
        let row3 = UIStackView(arrangedSubviews: [pinch, web]); row3.spacing = 8; row3.distribution = .fillEqually

        let table = UITableView(); table.accessibilityIdentifier = "dta_table"; table.dataSource = self; table.delegate = self
        table.register(UITableViewCell.self, forCellReuseIdentifier: "c")

        let stack = UIStackView(arrangedSubviews: [lbl, row1, custom, field, row2, host.view, row3, table]); stack.axis = .vertical; stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(stack)
        NSLayoutConstraint.activate([stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 8),
                                     stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
                                     stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
                                     stack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
                                     custom.heightAnchor.constraint(equalToConstant: 44),
                                     row3.heightAnchor.constraint(equalToConstant: 170)])
        dta("app ready")
    }
    override func viewWillTransition(to size: CGSize, with c: UIViewControllerTransitionCoordinator) {
        super.viewWillTransition(to: size, with: c); dta("rotate size=\(Int(size.width))x\(Int(size.height))")
    }
    func tableView(_ t: UITableView, numberOfRowsInSection s: Int) -> Int { 200 }
    func tableView(_ t: UITableView, cellForRowAt i: IndexPath) -> UITableViewCell {
        let c = t.dequeueReusableCell(withIdentifier: "c", for: i); c.textLabel?.text = "Item \(i.row + 1)"; return c
    }
    func tableView(_ t: UITableView, didSelectRowAt i: IndexPath) { dta("table select row=\(i.row + 1)"); t.deselectRow(at: i, animated: false) }
    func scrollViewDidEndDecelerating(_ s: UIScrollView) { dta("scroll end y=\(Int(s.contentOffset.y))") }
    func scrollViewDidEndScrollingAnimation(_ s: UIScrollView) { dta("scroll end y=\(Int(s.contentOffset.y))") }
    func scrollViewDidEndDragging(_ s: UIScrollView, willDecelerate d: Bool) { if !d { dta("scroll end y=\(Int(s.contentOffset.y))") } }
    func scrollViewWillBeginDragging(_ s: UIScrollView) { dta("scroll drag_began") }
    func textFieldShouldReturn(_ f: UITextField) -> Bool { dta("field return"); f.resignFirstResponder(); return true }
}

/// Tab 2: the keyboard and field cases S1 lacked.
final class FormsVC: UIViewController, UITextFieldDelegate {
    let masked = UITextField()

    func field(_ placeholder: String, _ id: String, _ keyboard: UIKeyboardType) -> UITextField {
        let f = UITextField(); f.borderStyle = .roundedRect; f.placeholder = placeholder; f.accessibilityIdentifier = id
        f.keyboardType = keyboard; f.autocorrectionType = .no; f.delegate = self
        f.addAction(UIAction { _ in dta("\(id) editing_began") }, for: .editingDidBegin)
        f.addAction(UIAction { _ in dta("\(id) editing_ended") }, for: .editingDidEnd)
        return f
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let title = UILabel(); title.text = "Forms"; title.accessibilityIdentifier = "dta_forms_title"
        title.accessibilityTraits = .header

        // a number pad has no Return key: one with a Done bar, one without
        let numDone = field("Number with Done", "dta_num_done", .numberPad)
        let bar = UIToolbar(); bar.sizeToFit()
        let done = UIBarButtonItem(systemItem: .done, primaryAction: UIAction { _ in
            dta("dta_num_done done"); numDone.resignFirstResponder()
        })
        done.accessibilityIdentifier = "dta_num_done_button"
        bar.items = [UIBarButtonItem(systemItem: .flexibleSpace), done]
        numDone.inputAccessoryView = bar
        numDone.addAction(UIAction { _ in dta("dta_num_done text=\(numDone.text ?? "")") }, for: .editingChanged)
        let numPlain = field("Number without Done", "dta_num_plain", .numberPad)
        numPlain.addAction(UIAction { _ in dta("dta_num_plain text=\(numPlain.text ?? "")") }, for: .editingChanged)

        // secure: the length is logged, never the text
        let secure = field("Password", "dta_secure", .default); secure.isSecureTextEntry = true
        secure.textContentType = .oneTimeCode            // keeps iOS's password autofill sheet away
        secure.addAction(UIAction { _ in dta("dta_secure length=\(secure.text?.count ?? 0)") }, for: .editingChanged)

        // masked: digits only, shown as (555) 123-4567, however the text arrives
        masked.borderStyle = .roundedRect; masked.placeholder = "Phone"; masked.accessibilityIdentifier = "dta_masked"
        masked.keyboardType = .phonePad; masked.delegate = self
        masked.addAction(UIAction { [weak self] _ in self?.reformat() }, for: .editingChanged)
        masked.addAction(UIAction { _ in dta("dta_masked editing_began") }, for: .editingDidBegin)

        let splash = button("Open splash", "dta_splash") { [weak self] in self?.openSplash() }

        let stack = UIStackView(arrangedSubviews: [title, numDone, numPlain, secure, masked, splash])
        stack.axis = .vertical; stack.spacing = 12; stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 8),
                                     stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
                                     stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16)])
    }

    override func viewDidAppear(_ animated: Bool) { super.viewDidAppear(animated); dta("forms shown") }

    func reformat() {
        let digits = String((masked.text ?? "").filter(\.isNumber).prefix(10))
        var out = ""
        for (i, c) in digits.enumerated() {
            if i == 0 { out += "(" }
            if i == 3 { out += ") " }
            if i == 6 { out += "-" }
            out.append(c)
        }
        if masked.text != out { masked.text = out }
        dta("dta_masked text=\(out)")
    }

    func textFieldShouldReturn(_ f: UITextField) -> Bool {
        dta("\(f.accessibilityIdentifier ?? "field") return"); f.resignFirstResponder(); return true
    }

    /// A splash (1.2 s, with a spinner) that hands over to a home screen: what an app's launch looks like.
    func openSplash() {
        let s = UIViewController(); s.view.backgroundColor = .systemIndigo; s.modalPresentationStyle = .fullScreen
        let l = UILabel(); l.text = "Loading…"; l.textColor = .white; l.accessibilityIdentifier = "dta_splash_label"
        let spin = UIActivityIndicatorView(style: .large); spin.color = .white; spin.startAnimating()
        let st = UIStackView(arrangedSubviews: [spin, l]); st.axis = .vertical; st.spacing = 12
        st.translatesAutoresizingMaskIntoConstraints = false; s.view.addSubview(st)
        NSLayoutConstraint.activate([st.centerXAnchor.constraint(equalTo: s.view.centerXAnchor),
                                     st.centerYAnchor.constraint(equalTo: s.view.centerYAnchor)])
        present(s, animated: false) { dta("splash shown") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            let home = UIViewController(); home.view.backgroundColor = .systemBackground; home.modalPresentationStyle = .fullScreen
            let w = UILabel(); w.text = "Welcome home"; w.accessibilityIdentifier = "dta_home_label"
            let close = button("Close", "dta_home_close") { [weak home] in home?.presentingViewController?.dismiss(animated: false) { dta("home closed") } }
            let hs = UIStackView(arrangedSubviews: [w, close]); hs.axis = .vertical; hs.spacing = 12
            hs.translatesAutoresizingMaskIntoConstraints = false; home.view.addSubview(hs)
            NSLayoutConstraint.activate([hs.centerXAnchor.constraint(equalTo: home.view.centerXAnchor),
                                         hs.centerYAnchor.constraint(equalTo: home.view.centerYAnchor)])
            s.dismiss(animated: false) { self.present(home, animated: false) { dta("home shown") } }
        }
    }
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ a: UIApplication, didFinishLaunchingWithOptions o: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let main = MainVC(); main.tabBarItem = UITabBarItem(title: "Main", image: UIImage(systemName: "hand.tap"), tag: 0)
        let forms = FormsVC(); forms.tabBarItem = UITabBarItem(title: "Forms", image: UIImage(systemName: "keyboard"), tag: 1)
        let tabs = UITabBarController(); tabs.viewControllers = [main, forms]
        window = UIWindow(frame: UIScreen.main.bounds); window?.rootViewController = tabs; window?.makeKeyAndVisible()
        return true
    }
}
