import com.openhtmltopdf.pdfboxout.PdfRendererBuilder;
import com.openhtmltopdf.svgsupport.BatikSVGDrawer;
import java.io.*;
import java.nio.file.*;
import java.util.TimeZone;

public class VtbGenerator {
    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("Usage: VtbGenerator <html_file> <output_pdf> <font_ttf>");
            System.exit(1);
        }

        TimeZone.setDefault(TimeZone.getTimeZone("Europe/Moscow"));

        String htmlPath = args[0];
        String outputPath = args[1];
        String fontPath = args[2];

        try (OutputStream os = new FileOutputStream(outputPath)) {
            PdfRendererBuilder builder = new PdfRendererBuilder();
            builder.useFastMode();
            builder.useFont(new File(fontPath), "SF Pro Display");
            builder.useSVGDrawer(new BatikSVGDrawer());
            builder.withUri(new File(htmlPath).toURI().toString());
            builder.toStream(os);
            builder.run();
        }

        System.out.println("OK: " + outputPath);
    }
}
